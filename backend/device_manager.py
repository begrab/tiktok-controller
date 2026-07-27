"""Device discovery and the runtime coordinate space.

Devices are discovered over HTTP via ``/device/get`` (the handover measured
local calls at ~70 ms and concluded the WebSocket interface is not worth the
complexity, so there is no persistent socket here).

The critical detail this module exists to enforce: **all tap coordinates and
all OCR ``centre`` values live in the ``imgw x imgh`` space** -- the mirror
stream resolution (608x1080 on the reference phone). That is neither the panel
resolution nor iOS logical points (``width``/``height``, 375x667), which are not
usable for taps. ``imgw``/``imgh`` are read at runtime, never hardcoded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from api_client import IMouseClient

logger = logging.getLogger("device_manager")


@dataclass
class Device:
    """A device as reported by ``/device/get``."""

    device_id: str
    name: str = ""
    model: str = "Unknown"
    version: str = ""            # iOS version
    state: int = 0              # 1 = online, 0 = offline

    # Coordinate space for taps / OCR centres (mirror stream resolution).
    imgw: int = 0
    imgh: int = 0

    # iOS logical points -- informational only, NOT usable for taps.
    width: int = 0
    height: int = 0

    ip: str = ""
    vid: str = ""
    pid: str = ""
    uid: str = ""
    last_seen: datetime = field(default_factory=datetime.now)

    @property
    def is_online(self) -> bool:
        return self.state == 1

    @property
    def has_coord_space(self) -> bool:
        return self.imgw > 0 and self.imgh > 0

    @staticmethod
    def _int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_api(cls, data: dict) -> "Device":
        """Build from a ``/device/get`` list entry."""
        device_id = data.get("deviceid") or data.get("id") or data.get("mac") or ""
        return cls(
            device_id=device_id,
            name=data.get("name") or data.get("username") or "",
            model=data.get("model") or "Unknown",
            version=str(data.get("version") or ""),
            state=cls._int(data.get("state"), 0),
            imgw=cls._int(data.get("imgw")),
            imgh=cls._int(data.get("imgh")),
            width=cls._int(data.get("width")),
            height=cls._int(data.get("height")),
            ip=data.get("ip") or "",
            vid=data.get("vid") or "",
            pid=data.get("pid") or "",
            uid=data.get("uid") or "",
            last_seen=datetime.now(),
        )

    def to_pixels(self, fx: float, fy: float) -> tuple[int, int]:
        """Map fractional coordinates ``(0..1, 0..1)`` into the tap space.

        Uses ``imgw``/``imgh`` -- the only coordinate space that produces correct
        taps. Falls back to logical points only if the mirror resolution is
        unknown (which usually means mirroring is not active).
        """
        w = self.imgw or self.width
        h = self.imgh or self.height
        return int(w * fx), int(h * fy)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "model": self.model,
            "version": self.version,
            "is_online": self.is_online,
            "state": self.state,
            # Report the coordinate space the frontend should reason about.
            "width": self.imgw or self.width,
            "height": self.imgh or self.height,
            "imgw": self.imgw,
            "imgh": self.imgh,
            "logical_width": self.width,
            "logical_height": self.height,
            "ip": self.ip,
            "bound": bool(self.vid or self.pid or self.uid),
        }


class DeviceRegistry:
    """Caches devices discovered via HTTP ``/device/get``."""

    def __init__(self, client: IMouseClient):
        self._client = client
        self._devices: dict[str, Device] = {}

    @property
    def devices(self) -> list[Device]:
        return list(self._devices.values())

    @property
    def online_devices(self) -> list[Device]:
        return [d for d in self._devices.values() if d.is_online]

    def get(self, device_id: str) -> Optional[Device]:
        return self._devices.get(device_id)

    async def refresh(self) -> list[Device]:
        """Re-query the console and refresh the cache.

        Stale offline entries from previously connected phones stay in the
        console's list, so callers should prefer :attr:`online_devices`.
        """
        raw = await self._client.get_devices()
        found: dict[str, Device] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            device = Device.from_api(entry)
            if device.device_id:
                found[device.device_id] = device
        self._devices = found

        online = [d for d in found.values() if d.is_online]
        logger.info("Device refresh: %d total, %d online", len(found), len(online))
        for d in online:
            if not d.has_coord_space:
                logger.warning(
                    "Device %s online but no imgw/imgh -- mirroring may be down; "
                    "taps will be unreliable.",
                    d.device_id,
                )
        return self.devices

    async def get_or_refresh(self, device_id: str) -> Optional[Device]:
        """Return a cached device, refreshing once if it is unknown."""
        device = self._devices.get(device_id)
        if device is None:
            await self.refresh()
            device = self._devices.get(device_id)
        return device

    def status(self) -> dict:
        return {
            "device_count": len(self._devices),
            "online_count": len(self.online_devices),
            "devices": [d.to_dict() for d in self._devices.values()],
        }
