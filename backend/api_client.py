"""PhoneFarm/iMouse API Client."""
import base64
import json
import asyncio
import logging
from typing import Optional, Any, Callable
from dataclasses import dataclass

import httpx
import websockets
from websockets.client import WebSocketClientProtocol

from config import Config

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("imouse_api")


@dataclass
class ApiResponse:
    success: bool
    status: int
    data: Any
    message: str


class PhoneFarmClient:
    """HTTP client for PhoneFarm/iMouse API."""

    # Ports to try in order (iMouseXP uses 9911, PhoneFarm uses 9912)
    FALLBACK_PORTS = [9911, 9912]

    def __init__(self, config: Config, device_manager: Any = None):
        self.config = config
        self.base_url = config.http_base_url
        self._client: Optional[httpx.AsyncClient] = None
        self._working_port: Optional[int] = None  # Cache the working port
        self._device_manager = device_manager  # For WebSocket fallback
        logger.info(f"PhoneFarmClient initialized with base_url: {self.base_url}")

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _get_url_for_port(self, port: int) -> str:
        """Get API URL for a specific port."""
        return f"http://{self.config.imouse_host}:{port}/api"

    async def _try_request(self, url: str, payload: dict) -> Optional[ApiResponse]:
        """Try a single request to a URL. Returns None if connection fails."""
        try:
            response = await self.client.post(url, json=payload)
            logger.debug(f"Response status code: {response.status_code}")
            logger.debug(f"Response body: {response.text[:500]}")

            result = response.json()

            return ApiResponse(
                success=result.get("status") == 0,
                status=result.get("status", -1),
                data=result.get("data"),
                message=result.get("message", "")
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            return None  # Connection failed, try next port
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from {url}: {e}")
            return ApiResponse(False, -1, None, f"Invalid JSON: {e}")
        except Exception as e:
            logger.error(f"Error in request to {url}: {e}")
            return ApiResponse(False, -1, None, str(e))

    async def _request(self, fun: str, data: dict = None, msgid: int = 0) -> ApiResponse:
        """Make an API request to iMouse with automatic port fallback."""
        payload = {
            "fun": fun,
            "msgid": msgid,
            "data": data or {}
        }

        logger.debug(f"Payload: {json.dumps(payload, indent=2)}")

        # If we already know the working port, use it
        if self._working_port:
            url = self._get_url_for_port(self._working_port)
            logger.debug(f"API Request to {url} (cached port)")
            result = await self._try_request(url, payload)
            if result is not None:
                if result.success:
                    logger.info(f"API call '{fun}' succeeded")
                else:
                    logger.warning(f"API call '{fun}' failed: status={result.status}, message={result.message}")
                return result
            else:
                # Cached port no longer works, reset and try fallback
                logger.warning(f"Cached port {self._working_port} no longer responding, trying fallback...")
                self._working_port = None

        # Try configured port first, then fallbacks
        ports_to_try = [self.config.imouse_http_port] + [p for p in self.FALLBACK_PORTS if p != self.config.imouse_http_port]

        for port in ports_to_try:
            url = self._get_url_for_port(port)
            logger.debug(f"API Request to {url}")

            result = await self._try_request(url, payload)

            if result is not None:
                # This port works! Cache it
                if self._working_port != port:
                    logger.info(f"iMouse API responding on port {port}")
                    self._working_port = port
                    self.base_url = url  # Update base URL

                if result.success:
                    logger.info(f"API call '{fun}' succeeded")
                else:
                    logger.warning(f"API call '{fun}' failed: status={result.status}, message={result.message}")
                return result
            else:
                logger.debug(f"Port {port} not responding, trying next...")

        # All ports failed
        logger.error(f"All connection attempts failed. Tried ports: {ports_to_try}")
        return ApiResponse(
            success=False,
            status=-1,
            data=None,
            message=f"Connection failed on all ports ({', '.join(map(str, ports_to_try))})"
        )

    # Device Management
    async def get_device_list(self) -> ApiResponse:
        """Get list of all devices from iMouse."""
        logger.info("Fetching device list from iMouse...")
        response = await self._request("get_device_list")

        if response.success:
            if response.data:
                device_count = len(response.data) if isinstance(response.data, dict) else 0
                logger.info(f"Found {device_count} device(s)")
                for dev_id, dev_info in (response.data.items() if isinstance(response.data, dict) else []):
                    logger.debug(f"  Device: {dev_id} -> {dev_info}")
            else:
                logger.warning("get_device_list succeeded but returned no data")
        else:
            logger.error(f"get_device_list failed: {response.message}")

        return response

    async def test_connection(self) -> dict:
        """Test connection to iMouse and return diagnostic info."""
        result = {
            "base_url": self.base_url,
            "working_port": self._working_port,
            "reachable": False,
            "api_works": False,
            "device_count": 0,
            "devices": [],
            "error": None,
            "raw_response": None
        }

        try:
            logger.info(f"Testing connection...")

            # Try the device list call (this will auto-detect working port)
            response = await self.get_device_list()

            # Update result with working port info
            result["working_port"] = self._working_port
            result["base_url"] = self.base_url

            if response.status != -1:  # Got a response (even if error)
                result["reachable"] = True

            result["raw_response"] = {
                "success": response.success,
                "status": response.status,
                "message": response.message,
                "data_type": type(response.data).__name__ if response.data else None,
                "data_preview": str(response.data)[:500] if response.data else None
            }

            # API works if we got a valid response (even with empty data)
            if response.success:
                result["api_works"] = True
                if response.data:
                    if isinstance(response.data, dict):
                        result["device_count"] = len(response.data)
                        result["devices"] = list(response.data.keys())
                    elif isinstance(response.data, list):
                        result["device_count"] = len(response.data)
                        result["devices"] = response.data

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"Connection test failed: {e}")

        return result

    async def set_device(self, device_id: str, **kwargs) -> ApiResponse:
        """Configure a device."""
        data = {"deviceid": device_id, **kwargs}
        return await self._request("set_dev", data)

    async def delete_device(self, device_id: str) -> ApiResponse:
        """Delete an offline device."""
        return await self._request("del_dev", {"deviceid": device_id})

    # Groups
    async def get_group_list(self) -> ApiResponse:
        """Get list of all groups."""
        return await self._request("get_group_list")

    async def set_group(self, gid: str = "0", name: str = "") -> ApiResponse:
        """Create or update a group."""
        return await self._request("set_group", {"gid": gid, "name": name})

    # Screenshots
    async def get_screenshot(
        self,
        device_id: str,
        as_jpeg: bool = True,
        gzip: bool = False
    ) -> ApiResponse:
        """Get a screenshot from device."""
        # Try HTTP first
        result = await self._request("get_device_screenshot", {
            "deviceid": device_id,
            "isjpg": as_jpeg,
            "gzip": gzip,
            "binary": False
        })

        # If HTTP fails with "List index out of bounds", try WebSocket
        if not result.success and "List index out of bounds" in result.message:
            if self._device_manager and self._device_manager.is_connected:
                logger.info("HTTP screenshot failed, trying WebSocket...")
                ws_result = await self._device_manager.ws_screenshot(device_id)
                return ApiResponse(
                    success=ws_result.get("success", False),
                    status=ws_result.get("status", -1),
                    data=ws_result.get("data"),
                    message=ws_result.get("message", "")
                )

        return result

    # Input Control
    async def click(
        self,
        device_id: str,
        x: int,
        y: int,
        button: str = "left",
        duration: int = 50
    ) -> ApiResponse:
        """Click at coordinates."""
        # Try HTTP first
        result = await self._request("click", {
            "deviceid": device_id,
            "x": x,
            "y": y,
            "button": button,
            "time": duration
        })

        # If HTTP fails with "List index out of bounds", try WebSocket
        if not result.success and "List index out of bounds" in result.message:
            if self._device_manager and self._device_manager.is_connected:
                logger.info("HTTP click failed, trying WebSocket...")
                ws_result = await self._device_manager.ws_click(device_id, x, y, duration)
                return ApiResponse(
                    success=ws_result.get("success", False),
                    status=ws_result.get("status", -1),
                    data=None,
                    message=ws_result.get("message", "")
                )

        return result

    async def swipe(
        self,
        device_id: str,
        direction: str = "up",
        length: float = 0.5,
        start_x: Optional[int] = None,
        start_y: Optional[int] = None,
        end_x: Optional[int] = None,
        end_y: Optional[int] = None
    ) -> ApiResponse:
        """Swipe in a direction or between coordinates."""
        data = {
            "deviceid": device_id,
            "direction": direction,
            "length": length,
            "button": "left"
        }
        if start_x is not None:
            data["sx"] = start_x
        if start_y is not None:
            data["sy"] = start_y
        if end_x is not None:
            data["ex"] = end_x
        if end_y is not None:
            data["ey"] = end_y

        # Try HTTP first
        result = await self._request("swipe", data)

        # If HTTP fails with "List index out of bounds", try WebSocket
        if not result.success and "List index out of bounds" in result.message:
            if self._device_manager and self._device_manager.is_connected:
                logger.info("HTTP swipe failed, trying WebSocket...")
                ws_result = await self._device_manager.ws_swipe(device_id, direction, length)
                return ApiResponse(
                    success=ws_result.get("success", False),
                    status=ws_result.get("status", -1),
                    data=None,
                    message=ws_result.get("message", "")
                )

        return result

    async def mouse_move(self, device_id: str, x: int, y: int) -> ApiResponse:
        """Move mouse to coordinates."""
        return await self._request("mouse_move", {
            "deviceid": device_id,
            "x": x,
            "y": y
        })

    async def mouse_down(self, device_id: str, button: str = "left") -> ApiResponse:
        """Press mouse button."""
        return await self._request("mouse_down", {
            "deviceid": device_id,
            "button": button
        })

    async def mouse_up(self, device_id: str, button: str = "left") -> ApiResponse:
        """Release mouse button."""
        return await self._request("mouse_up", {
            "deviceid": device_id,
            "button": button
        })

    async def mouse_reset(self, device_id: str) -> ApiResponse:
        """Reset mouse position."""
        return await self._request("mouse_reset_pos", {"deviceid": device_id})

    # Keyboard
    async def send_key(
        self,
        device_id: str,
        key: Optional[str] = None,
        fn_key: Optional[str] = None
    ) -> ApiResponse:
        """Send keyboard input."""
        data = {"deviceid": device_id}
        if key:
            data["key"] = key
        if fn_key:
            data["fn_key"] = fn_key
        return await self._request("send_key", data)

    async def send_text(
        self,
        device_id: str,
        text: str,
        trigger_hotkey: str = "CTRL+ALT+SHIFT+WIN+v"
    ) -> ApiResponse:
        """Send text input (for Chinese/unicode text)."""
        return await self._request("send_text", {
            "deviceid": device_id,
            "key": text,
            "fn_key": trigger_hotkey
        })

    async def key_down(self, device_id: str, key: str) -> ApiResponse:
        """Press a key down."""
        return await self._request("key_down", {"deviceid": device_id, "key": key})

    async def key_up(self, device_id: str, key: str) -> ApiResponse:
        """Release a key."""
        return await self._request("key_up", {"deviceid": device_id, "key": key})

    async def release_all_keys(self, device_id: str) -> ApiResponse:
        """Release all pressed keys."""
        return await self._request("key_release_all", {"deviceid": device_id})

    # OCR and Image Recognition
    async def ocr(
        self,
        device_id: str,
        rect: Optional[list] = None,
        enhanced: bool = False
    ) -> ApiResponse:
        """Perform OCR on device screen."""
        data = {"deviceid": device_id}
        if rect:
            data["rect"] = rect
        fun = "ocr_ex" if enhanced else "ocr"
        return await self._request(fun, data)

    async def find_image(
        self,
        device_id: str,
        image_base64: str,
        rect: Optional[list] = None,
        similarity: float = 0.9
    ) -> ApiResponse:
        """Find an image on screen."""
        data = {
            "deviceid": device_id,
            "img": image_base64,
            "similarity": similarity
        }
        if rect:
            data["rect"] = rect
        return await self._request("find_image", data)

    # Shortcuts
    async def shortcut(
        self,
        device_id: str,
        shortcut_id: int,
        devices: Optional[list] = None,
        timeout: int = 30,
        parameter: Optional[dict] = None
    ) -> ApiResponse:
        """Execute a device shortcut."""
        data = {
            "deviceid": device_id,
            "id": shortcut_id,
            "outtime": timeout
        }
        if devices:
            data["devlist"] = devices
        if parameter:
            data["parameter"] = json.dumps(parameter)
        return await self._request("shortcut", data)

    # Device Control
    async def restart_device(self, device_id: str) -> ApiResponse:
        """Restart the device."""
        return await self._request("restart_device", {"deviceid": device_id})

    async def restart_usb(self, device_id: str) -> ApiResponse:
        """Restart USB connection."""
        return await self._request("restart_usb", {"deviceid": device_id})


class PhoneFarmWebSocket:
    """WebSocket client for real-time updates and screenshot streaming."""

    def __init__(self, config: Config):
        self.config = config
        self.ws_url = config.ws_url
        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._callbacks: dict[str, list[Callable]] = {}

    async def connect(self):
        """Connect to WebSocket server."""
        self._ws = await websockets.connect(self.ws_url)
        self._running = True

    async def disconnect(self):
        """Disconnect from WebSocket server."""
        self._running = False
        if self._ws:
            await self._ws.close()

    def on(self, event: str, callback: Callable):
        """Register event callback."""
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)

    async def _emit(self, event: str, data: Any):
        """Emit event to callbacks."""
        for callback in self._callbacks.get(event, []):
            if asyncio.iscoroutinefunction(callback):
                await callback(data)
            else:
                callback(data)

    async def send(self, fun: str, data: dict = None, msgid: int = 0):
        """Send a message."""
        if self._ws:
            payload = {
                "fun": fun,
                "msgid": msgid,
                "data": data or {}
            }
            await self._ws.send(json.dumps(payload))

    async def start_screenshot_loop(
        self,
        device_id: str,
        interval_ms: int = 100,
        as_jpeg: bool = True
    ):
        """Start continuous screenshot streaming."""
        await self.send("loop_device_screenshot", {
            "deviceid": device_id,
            "time": interval_ms,
            "isjpg": as_jpeg
        })

    async def stop_screenshot_loop(self, device_id: str):
        """Stop screenshot streaming."""
        await self.send("loop_device_screenshot", {
            "deviceid": device_id,
            "stop": True
        })

    async def listen(self):
        """Listen for incoming messages."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        while self._running:
            try:
                message = await self._ws.recv()
                if isinstance(message, bytes):
                    # Binary screenshot data
                    await self._emit("screenshot", message)
                else:
                    # JSON message
                    data = json.loads(message)
                    fun = data.get("fun", "unknown")
                    await self._emit(fun, data)
                    await self._emit("message", data)
            except websockets.exceptions.ConnectionClosed:
                await self._emit("disconnected", None)
                break
            except Exception as e:
                await self._emit("error", str(e))
