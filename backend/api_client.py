"""iMouse XP API client.

Thin async wrapper over the iMouse XP console's local HTTP API, matching the
verified behaviour documented in the technical handover:

  * Base URL is ``http://127.0.0.1:9911/api``. There is no API key; auth is
    "being on localhost". Port 9912 (seen in some third-party guides) is wrong.
  * Every request is ``POST {"fun": <endpoint>, "data": {...}}``.
  * A call succeeded only if HTTP ``status == 200`` AND ``data.code == 0``.
    An HTTP 200 with a non-zero ``data.code`` is a failure -- check both.
  * ``/pic/screenshot`` with ``binary: true`` returns raw JPEG bytes, not JSON.
    Branch on the response content type before parsing.

The WebSocket interface is intentionally not used: the handover measured local
HTTP calls at ~70 ms and concluded the WebSocket is not worth the added
complexity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from config import Config

logger = logging.getLogger("imouse")


# Error codes documented in the handover / API overview.
ERROR_MESSAGES = {
    0: "success",
    28: "plugin not connected (the PaddleOCR console process is not running)",
}


class IMouseError(RuntimeError):
    """Raised when an iMouse call fails (transport, HTTP, or ``data.code != 0``)."""

    def __init__(self, fun: str, code: int, message: str, status: Optional[int] = None):
        self.fun = fun
        self.code = code
        self.message = message
        self.status = status
        hint = ERROR_MESSAGES.get(code)
        detail = f"{message}" if message else ""
        if hint and hint not in detail:
            detail = f"{detail} ({hint})".strip()
        super().__init__(f"{fun} failed [code={code}]: {detail}")


@dataclass
class ApiResult:
    """Successful result of an iMouse call.

    ``data`` is the inner ``data`` object from the response (containing ``code``,
    ``message`` and usually ``list``). ``items`` is a convenience view of
    ``data['list']`` when present.
    """

    fun: str
    data: dict

    @property
    def items(self) -> list:
        value = self.data.get("list")
        return value if isinstance(value, list) else []

    @property
    def message(self) -> str:
        return self.data.get("message", "")


class IMouseClient:
    """Async HTTP client for the iMouse XP local API."""

    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.http_base_url
        self._client: Optional[httpx.AsyncClient] = None
        logger.info("IMouseClient initialised with base_url: %s", self.base_url)

    async def __aenter__(self) -> "IMouseClient":
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # ------------------------------------------------------------------ core --
    async def _post(self, fun: str, data: Optional[dict] = None) -> httpx.Response:
        payload = {"fun": fun, "data": data or {}}
        logger.debug("POST %s <- %s", fun, payload)
        response = await self.client.post(self.base_url, json=payload)
        response.raise_for_status()
        return response

    async def call(self, fun: str, **data) -> ApiResult:
        """Make a JSON API call. Raises :class:`IMouseError` on any failure.

        Do not use this for ``/pic/screenshot`` with ``binary: true`` -- that
        returns raw bytes; use :meth:`screenshot` instead.
        """
        try:
            response = await self._post(fun, data)
        except httpx.HTTPError as exc:
            raise IMouseError(fun, -1, f"transport error: {exc}") from exc

        body = response.json()
        status = body.get("status")
        inner = body.get("data") or {}
        code = inner.get("code")

        if status != 200 or code != 0:
            raise IMouseError(
                fun,
                code if code is not None else -1,
                inner.get("message", ""),
                status=status,
            )

        logger.debug("%s ok", fun)
        return ApiResult(fun=fun, data=inner)

    # ------------------------------------------------------------- devices ----
    async def get_devices(self) -> list[dict]:
        """Return every device the console has ever seen (``/device/get``).

        Filter on ``state == 1`` for devices that are currently online. All tap
        coordinates live in the ``imgw x imgh`` space reported per device.
        """
        result = await self.call("/device/get")
        return result.items

    # ----------------------------------------------------------- screenshots --
    async def screenshot(
        self,
        device_id: str,
        *,
        binary: bool = True,
        rect: Optional[list[int]] = None,
        save_path: Optional[str] = None,
    ) -> bytes:
        """Capture the mirrored screen as JPEG bytes (``/pic/screenshot``).

        With ``binary=True`` the API returns raw JPEG bytes rather than JSON, so
        this method branches on the response content type. With ``binary=False``
        the JPEG comes back base64-encoded inside JSON; we decode it so callers
        always receive bytes.
        """
        data: dict[str, Any] = {"id": device_id, "jpg": True, "binary": binary}
        if rect:
            data["rect"] = rect
        if save_path:
            data["save_path"] = save_path

        try:
            response = await self._post("/pic/screenshot", data)
        except httpx.HTTPError as exc:
            raise IMouseError("/pic/screenshot", -1, f"transport error: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response.content  # raw JPEG bytes

        # JSON path: either a base64 image, or an error we must surface.
        body = response.json()
        inner = body.get("data") or {}
        if body.get("status") != 200 or inner.get("code") != 0:
            raise IMouseError(
                "/pic/screenshot",
                inner.get("code", -1),
                inner.get("message", ""),
                status=body.get("status"),
            )
        import base64

        b64 = inner.get("img") or inner.get("data") or ""
        return base64.b64decode(b64) if b64 else b""

    # -------------------------------------------------------------- input -----
    async def click(self, device_id: str, x: int, y: int) -> ApiResult:
        """Tap at ``(x, y)`` in the device's ``imgw x imgh`` coordinate space."""
        return await self.call("/mouse/click", id=device_id, x=int(x), y=int(y))

    async def swipe(
        self, device_id: str, direction: str = "up", length: float = 0.5
    ) -> ApiResult:
        """Swipe ``up``/``down``/``left``/``right``; ``length`` is a screen fraction."""
        return await self.call(
            "/mouse/swipe", id=device_id, direction=direction, len=length
        )

    async def wheel(
        self, device_id: str, direction: str = "up", length: float = 0.5, number: int = 1
    ) -> ApiResult:
        return await self.call(
            "/mouse/wheel",
            id=device_id,
            direction=direction,
            len=length,
            number=number,
        )

    # ------------------------------------------------------------- keyboard ---
    async def send_key(
        self,
        device_id: str,
        key: Optional[str] = None,
        fn_key: Optional[str] = None,
    ) -> ApiResult:
        """Send keyboard input (``/key/sendkey``).

        ``key`` is ASCII only -- accented characters and emoji are dropped. For
        non-ASCII text use :meth:`clipboard_set` then paste with ``fn_key='WIN+v'``.
        ``fn_key`` triggers a hotkey (e.g. ``WIN+h`` Home, ``TAB+b`` Back,
        ``ENTER``, ``BACKSPACE``, ``WIN+v`` Paste).
        """
        data: dict[str, Any] = {"id": device_id}
        if key is not None:
            data["key"] = key
        if fn_key is not None:
            data["fn_key"] = fn_key
        return await self.call("/key/sendkey", **data)

    # -------------------------------------------------------------- vision ----
    async def ocr(
        self,
        device_id: str,
        rect: Optional[list[int]] = None,
        enhanced: bool = False,
    ) -> ApiResult:
        """OCR the screen (``/pic/ocr`` or the heavier ``/pic/ocr-ex``).

        Each result carries ``centre: [x, y]`` (tap target) and ``rect``.
        Returns code 28 until the PaddleOCR plugin process is running.
        ``rect`` crops the processed region to cut OCR time.
        """
        data: dict[str, Any] = {"id": device_id}
        if rect:
            data["rect"] = rect
        return await self.call("/pic/ocr-ex" if enhanced else "/pic/ocr", **data)

    async def find_text(
        self,
        device_id: str,
        text: list[str],
        similarity: float = 0.8,
        contain: bool = True,
        rect: Optional[list[int]] = None,
    ) -> list[dict]:
        """Locate on-screen text and return matches with a tap ``centre``.

        Preferred over hardcoded coordinates for anything with a text label.
        Lower ``similarity`` for accented/stylised type -- the bundled model is
        tuned for Chinese and English.
        """
        data: dict[str, Any] = {
            "id": device_id,
            "text": text,
            "similarity": similarity,
            "contain": contain,
        }
        if rect:
            data["rect"] = rect
        result = await self.call("/pic/find-text", **data)
        return result.items

    async def find_image(
        self,
        device_id: str,
        image_base64: str,
        similarity: float = 0.9,
        rect: Optional[list[int]] = None,
    ) -> list[dict]:
        """Template-match an icon (``/pic/find-image``).

        Requires the OpenCV plugin, which the handover notes is not installed on
        the reference machine -- expect a failure until it is.
        """
        data: dict[str, Any] = {
            "id": device_id,
            "img": image_base64,
            "similarity": similarity,
        }
        if rect:
            data["rect"] = rect
        result = await self.call("/pic/find-image", **data)
        return result.items

    # ----------------------------------------------------------- shortcuts ----
    async def clipboard_set(self, device_id: str, text: str, sleep: int = 0) -> ApiResult:
        """Push UTF-8 text to the phone clipboard (``/shortcut/clipboard/set``).

        The reliable way to enter non-ASCII text: set the clipboard, focus the
        field, brief pause, then send ``fn_key='WIN+v'``.
        """
        return await self.call(
            "/shortcut/clipboard/set", id=device_id, text=text, sleep=sleep
        )

    async def clipboard_get(self, device_id: str) -> ApiResult:
        return await self.call("/shortcut/clipboard/get", id=device_id)

    async def exec_url(self, device_id: str, url: str) -> ApiResult:
        """Open a URL or deep link on the phone (``/shortcut/exec/url``)."""
        return await self.call("/shortcut/exec/url", id=device_id, url=url)

    async def device_ip(self, device_id: str) -> ApiResult:
        return await self.call("/shortcut/device/ip", id=device_id)
