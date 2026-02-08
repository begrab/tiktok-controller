"""WebSocket-based device manager for iMouse/PhoneFarm."""
import asyncio
import json
import logging
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

logger = logging.getLogger("device_manager")


@dataclass
class Device:
    """Represents a connected iMouse device."""
    device_id: str
    name: str = ""
    model: str = "Unknown"
    width: int = 0
    height: int = 0
    state: int = 0  # 1 = online, 0 = offline
    ip: str = ""
    mac: str = ""
    rotate: int = 0
    vid: str = ""
    pid: str = ""
    gid: str = ""
    last_seen: datetime = field(default_factory=datetime.now)

    @property
    def is_online(self) -> bool:
        return self.state == 1

    @classmethod
    def from_callback(cls, data: dict) -> "Device":
        """Create Device from WebSocket callback data."""
        # iMouseXP uses various field names for device ID
        device_id = (
            data.get("deviceid") or
            data.get("device_id") or
            data.get("id") or
            data.get("udid") or  # iPhone UDID
            data.get("mac") or
            ""
        )

        # Log the raw data for debugging
        logger.debug(f"Parsing device data: {list(data.keys())}")

        # Extract dimensions - iMouseXP may use different field names
        # Ensure these are integers (API may return strings)
        width = int(data.get("width") or data.get("screen_width") or 0)
        height = int(data.get("height") or data.get("screen_height") or 0)

        # State: 1 = online, 0 = offline
        # iMouseXP may send state in different ways
        state = data.get("state")
        if state is None:
            # If no state field, assume online if we're receiving device info
            state = 1

        return cls(
            device_id=device_id,
            name=data.get("name") or data.get("device_name") or data.get("username") or "",
            model=data.get("model") or data.get("device_model") or "Unknown",
            width=width,
            height=height,
            state=state,
            ip=data.get("ip") or data.get("device_ip") or "",
            mac=data.get("mac") or "",
            rotate=int(data.get("rotate") or 0),
            vid=data.get("vid") or "",
            pid=data.get("pid") or "",
            gid=data.get("gid") or "",
            last_seen=datetime.now()
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "model": self.model,
            "width": self.width,
            "height": self.height,
            "is_online": self.is_online,
            "ip": self.ip,
            "state": self.state,
        }


class DeviceManager:
    """
    Manages device discovery via WebSocket connection to iMouse.

    Maintains a registry of devices populated from:
    1. WebSocket callbacks (dev_connect, dev_disconnect)
    2. Explicit get_device_list requests via WebSocket
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9911):
        self.host = host
        self.port = port
        # Note: iMouse uses "clinet" (typo in their API)
        self.ws_url = f"ws://{host}:{port}/clinet"

        self._ws: Optional[WebSocketClientProtocol] = None
        self._devices: dict[str, Device] = {}
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._msgid = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._on_device_change: Optional[Callable[[str, Device], Any]] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._connected = False

    @property
    def devices(self) -> list[Device]:
        """Get list of all known devices."""
        return list(self._devices.values())

    @property
    def online_devices(self) -> list[Device]:
        """Get list of online devices only."""
        return [d for d in self._devices.values() if d.is_online]

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected and self._ws is not None

    def get_device(self, device_id: str) -> Optional[Device]:
        """Get a specific device by ID."""
        return self._devices.get(device_id)

    def on_device_change(self, callback: Callable[[str, Device], Any]):
        """Register callback for device changes. Args: (event_type, device)"""
        self._on_device_change = callback

    async def start(self):
        """Start the WebSocket connection and listener."""
        if self._running:
            return

        self._running = True
        self._listen_task = asyncio.create_task(self._connection_loop())
        logger.info(f"DeviceManager started, connecting to {self.ws_url}")

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._connected = False
        logger.info("DeviceManager stopped")

    async def _connection_loop(self):
        """Main connection loop with automatic reconnection."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay
                )

    async def _connect_and_listen(self):
        """Connect to WebSocket and listen for messages."""
        logger.info(f"Connecting to {self.ws_url}...")

        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self._connected = True
            self._reconnect_delay = 1.0  # Reset on successful connection
            logger.info("WebSocket connected successfully")

            # Request initial device list via WebSocket
            await self._request_device_list()

            # Also try HTTP API as fallback for already-connected devices
            await self._fetch_devices_via_http()

            # Listen for messages
            async for message in self._ws:
                await self._handle_message(message)

        except ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}")
            self._connected = False
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            self._connected = False
            raise
        finally:
            self._ws = None
            self._connected = False

    async def _fetch_devices_via_http(self):
        """Fallback: Try to get devices via HTTP API."""
        import httpx

        url = f"http://{self.host}:{self.port}/api"
        payload = {"fun": "get_device_list", "msgid": 0, "data": {}}

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                result = response.json()

                logger.info(f"HTTP get_device_list response: status={result.get('status')}, keys={list(result.get('data', {}).keys()) if isinstance(result.get('data'), dict) else 'N/A'}")

                if result.get("status") == 0 and result.get("data"):
                    await self._handle_device_list(result["data"])
                else:
                    logger.debug(f"HTTP device list empty or failed: {result.get('message', 'unknown')}")

        except Exception as e:
            logger.debug(f"HTTP fallback failed (this is OK): {e}")

    async def _handle_message(self, message: str | bytes):
        """Handle incoming WebSocket message."""
        if isinstance(message, bytes):
            # Binary message (screenshot data) - ignore for device management
            return

        try:
            data = json.loads(message)
            fun = data.get("fun", "")
            msg_data = data.get("data", {})
            msgid = data.get("msgid", 0)
            status = data.get("status", -1)

            # Log ALL messages for debugging
            logger.info(f"WS message: fun={fun}, status={status}, msgid={msgid}, data_keys={list(msg_data.keys()) if isinstance(msg_data, dict) else type(msg_data)}")

            # Handle pending request responses
            if msgid in self._pending_requests:
                future = self._pending_requests.pop(msgid)
                if not future.done():
                    future.set_result(data)
                # Don't return - also process as callback

            # Handle async callbacks based on function name
            if fun == "dev_connect":
                await self._handle_device_connect(msg_data)
            elif fun == "dev_disconnect":
                await self._handle_device_disconnect(msg_data)
            elif fun == "dev_rotate":
                await self._handle_device_rotate(msg_data)
            elif fun == "get_device_list":
                await self._handle_device_list(msg_data)
            elif fun == "user_info":
                # User/license info - might contain device list
                if "list" in msg_data:
                    logger.info(f"user_info contains device list with {len(msg_data.get('list', []))} items")
                    await self._handle_device_list(msg_data)
            else:
                # Unknown function - log it and check if it contains devices
                logger.debug(f"Unknown WS function: {fun}")
                # Try to extract devices from any message that has a list
                if isinstance(msg_data, dict) and "list" in msg_data:
                    logger.info(f"Message '{fun}' contains list, trying to parse as devices")
                    await self._handle_device_list(msg_data)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON message: {e}")
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

    async def _handle_device_connect(self, data: dict):
        """Handle dev_connect callback."""
        # iMouseXP sends devices in data.list[] array
        devices_to_process = []

        if "list" in data and isinstance(data["list"], list):
            # Format: {"list": [{device1}, {device2}, ...]}
            devices_to_process = data["list"]
            logger.debug(f"dev_connect contains {len(devices_to_process)} device(s) in list")
        elif data.get("deviceid") or data.get("mac"):
            # Format: {device info directly}
            devices_to_process = [data]
        else:
            logger.warning(f"dev_connect: unexpected data format: {list(data.keys())}")
            return

        for device_data in devices_to_process:
            device = Device.from_callback(device_data)
            device.state = 1  # Ensure online

            if device.device_id:
                self._devices[device.device_id] = device
                logger.info(f"Device connected: {device.device_id} ({device.model}) - {device.width}x{device.height}")

                if self._on_device_change:
                    try:
                        result = self._on_device_change("connect", device)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"Error in device change callback: {e}")

    async def _handle_device_disconnect(self, data: dict):
        """Handle dev_disconnect callback."""
        # iMouseXP may send devices in data.list[] array
        devices_to_process = []

        if "list" in data and isinstance(data["list"], list):
            devices_to_process = data["list"]
        elif data.get("deviceid") or data.get("mac"):
            devices_to_process = [data]
        else:
            logger.warning(f"dev_disconnect: unexpected data format")
            return

        for device_data in devices_to_process:
            device = Device.from_callback(device_data)
            device.state = 0  # Ensure offline

            if device.device_id:
                if device.device_id in self._devices:
                    self._devices[device.device_id].state = 0
                    self._devices[device.device_id].last_seen = datetime.now()
                else:
                    self._devices[device.device_id] = device

                logger.info(f"Device disconnected: {device.device_id}")

                if self._on_device_change:
                    try:
                        result = self._on_device_change("disconnect", device)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"Error in device change callback: {e}")

    async def _handle_device_rotate(self, data: dict):
        """Handle dev_rotate callback."""
        device_id = data.get("deviceid")
        if device_id and device_id in self._devices:
            self._devices[device_id].rotate = data.get("rotate", 0)
            # Width/height may swap on rotation
            if "width" in data:
                self._devices[device_id].width = data["width"]
            if "height" in data:
                self._devices[device_id].height = data["height"]
            logger.debug(f"Device rotated: {device_id}")

    async def _handle_device_list(self, data: dict):
        """Handle get_device_list response."""
        if not data:
            logger.debug("Device list response is empty")
            return

        logger.debug(f"Processing device list data: {json.dumps(data)[:500]}")

        devices_found = 0

        # Format 1: {"list": [{device1}, {device2}, ...]}
        if "list" in data and isinstance(data["list"], list):
            logger.info(f"Found 'list' array with {len(data['list'])} items")
            for i, device_data in enumerate(data["list"]):
                if isinstance(device_data, dict):
                    logger.debug(f"Device {i} keys: {list(device_data.keys())}")
                    device = Device.from_callback(device_data)
                    if device.device_id:
                        self._devices[device.device_id] = device
                        devices_found += 1
                        logger.info(f"Added device: {device.device_id} ({device.model}) {device.width}x{device.height}")
                    else:
                        logger.warning(f"Device {i} has no device_id, raw data: {list(device_data.keys())}")

        # Format 2: {"device_id": {device_info}, ...}
        elif isinstance(data, dict):
            for device_id, device_data in data.items():
                if device_id in ("list", "code", "status", "pass", "total_license", "use_license", "id"):
                    continue
                if isinstance(device_data, dict):
                    device_data["deviceid"] = device_id
                    device = Device.from_callback(device_data)
                    self._devices[device_id] = device
                    devices_found += 1
                    logger.info(f"Added device: {device_id} ({device.model})")

        if devices_found > 0:
            logger.info(f"Device list updated: {devices_found} device(s) found, {len(self._devices)} total")
        else:
            logger.debug(f"No devices found in data")

    async def _request_device_list(self):
        """Request device list via WebSocket."""
        if not self._ws:
            return

        try:
            await self._send({"fun": "get_device_list", "data": {}})
            logger.debug("Requested device list via WebSocket")
        except Exception as e:
            logger.error(f"Failed to request device list: {e}")

    async def _send(self, payload: dict) -> Optional[dict]:
        """Send a message via WebSocket."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        self._msgid += 1
        payload["msgid"] = self._msgid

        await self._ws.send(json.dumps(payload))
        return None  # Async, response comes via callback

    async def request_with_response(self, fun: str, data: dict = None, timeout: float = 10.0) -> Optional[dict]:
        """Send a request and wait for response."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        self._msgid += 1
        msgid = self._msgid

        payload = {
            "fun": fun,
            "msgid": msgid,
            "data": data or {}
        }

        # Create future for response
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[msgid] = future

        try:
            await self._ws.send(json.dumps(payload))
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(msgid, None)
            logger.error(f"Request timeout: {fun}")
            return None
        except Exception as e:
            self._pending_requests.pop(msgid, None)
            logger.error(f"Request error: {e}")
            raise

    async def refresh_devices(self) -> list[Device]:
        """Force refresh device list."""
        # Try WebSocket first
        await self._request_device_list()
        await asyncio.sleep(0.3)

        # If no devices found, try HTTP fallback
        if not self._devices:
            await self._fetch_devices_via_http()

        return self.devices

    def get_status(self) -> dict:
        """Get manager status for debugging."""
        return {
            "ws_url": self.ws_url,
            "connected": self.is_connected,
            "device_count": len(self._devices),
            "online_count": len(self.online_devices),
            "devices": [d.to_dict() for d in self._devices.values()]
        }

    # WebSocket-based control commands (alternative to HTTP API)
    async def ws_swipe(
        self,
        device_id: str,
        direction: str = "up",
        length: float = 0.5
    ) -> dict:
        """Send swipe command via WebSocket."""
        if not self._ws:
            return {"success": False, "message": "WebSocket not connected"}

        try:
            result = await self.request_with_response("swipe", {
                "deviceid": device_id,
                "direction": direction,
                "length": length,
                "button": "left"
            })
            if result:
                return {
                    "success": result.get("status") == 0,
                    "status": result.get("status", -1),
                    "message": result.get("message", "")
                }
            return {"success": False, "message": "Request timeout"}
        except Exception as e:
            logger.error(f"WS swipe error: {e}")
            return {"success": False, "message": str(e)}

    async def ws_click(
        self,
        device_id: str,
        x: int,
        y: int,
        duration: int = 50
    ) -> dict:
        """Send click command via WebSocket."""
        if not self._ws:
            return {"success": False, "message": "WebSocket not connected"}

        try:
            result = await self.request_with_response("click", {
                "deviceid": device_id,
                "x": x,
                "y": y,
                "button": "left",
                "time": duration
            })
            if result:
                return {
                    "success": result.get("status") == 0,
                    "status": result.get("status", -1),
                    "message": result.get("message", "")
                }
            return {"success": False, "message": "Request timeout"}
        except Exception as e:
            logger.error(f"WS click error: {e}")
            return {"success": False, "message": str(e)}

    async def ws_screenshot(self, device_id: str) -> dict:
        """Get screenshot via WebSocket."""
        if not self._ws:
            return {"success": False, "message": "WebSocket not connected"}

        try:
            result = await self.request_with_response("get_device_screenshot", {
                "deviceid": device_id,
                "isjpg": True,
                "gzip": False,
                "binary": False
            })
            if result:
                return {
                    "success": result.get("status") == 0,
                    "status": result.get("status", -1),
                    "data": result.get("data"),
                    "message": result.get("message", "")
                }
            return {"success": False, "message": "Request timeout"}
        except Exception as e:
            logger.error(f"WS screenshot error: {e}")
            return {"success": False, "message": str(e)}
