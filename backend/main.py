"""FastAPI server for the TikTok controller web interface."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_agent import TikTokAgent
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import DeviceRegistry
from tiktok_actions import TikTokController
from ui_map import load_map, save_screen

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

config = Config.from_env()
client: Optional[IMouseClient] = None
registry: Optional[DeviceRegistry] = None
controller: Optional[TikTokController] = None
agent: Optional[TikTokAgent] = None
campaign_task: Optional[asyncio.Task] = None
ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, registry, controller, agent
    logger.info("iMouse API: %s (Claude model: %s)", config.http_base_url, config.anthropic_model)

    client = IMouseClient(config)
    await client.__aenter__()
    registry = DeviceRegistry(client)
    controller = TikTokController(config, client, registry)
    agent = TikTokAgent(controller)

    try:
        await registry.refresh()
    except IMouseError as exc:
        logger.warning("Initial device refresh failed (is the console running?): %s", exc)

    logger.info("Application started")
    yield
    await client.aclose()
    logger.info("Application stopped")


app = FastAPI(
    title="TikTok Accessibility Controller",
    description="Control TikTok on iOS devices via iMouse XP",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ models ----
class ConfigUpdate(BaseModel):
    imouse_host: Optional[str] = None
    imouse_port: Optional[int] = None
    anthropic_api_key: Optional[str] = None
    anthropic_model: Optional[str] = None


class CommentRequest(BaseModel):
    device_id: str
    comment: Optional[str] = None
    use_ai: bool = True
    ai_style: str = "friendly"


class BatchActionRequest(BaseModel):
    device_ids: list[str]
    action: str


class GenerateCommentRequest(BaseModel):
    device_id: str
    count: int = 3
    styles: Optional[list[str]] = None


class AutoBrowseRequest(BaseModel):
    device_id: str
    video_count: int = 10
    like_probability: float = 0.3
    comment_probability: float = 0.1
    min_watch_time: float = 3.0
    max_watch_time: float = 15.0


class UICalibrationUpdate(BaseModel):
    element: str
    x: float
    y: float
    w: Optional[float] = None
    h: Optional[float] = None


class AIAgentRequest(BaseModel):
    goal: Optional[str] = None
    max_steps: int = 10


class CampaignRequest(BaseModel):
    device_id: str
    duration_minutes: float = 30.0


class UIMapSave(BaseModel):
    screen_type: str
    elements: dict  # {label: [x, y]}
    imgw: int = 0
    imgh: int = 0


class TapRequest(BaseModel):
    x: int
    y: int


class ManualTypeRequest(BaseModel):
    text: str


class ManualKeyRequest(BaseModel):
    key: Optional[str] = None
    fn_key: Optional[str] = None


class RawCommand(BaseModel):
    fun: str
    data: Optional[dict] = None


# -------------------------------------------------------------- diagnostics ---
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": app.version}


@app.get("/api/debug/connection")
async def debug_connection():
    """Probe the iMouse console and report what came back."""
    info = {
        "base_url": config.http_base_url,
        "reachable": False,
        "device_count": 0,
        "online_count": 0,
        "error": None,
    }
    try:
        devices = await registry.refresh()
        info["reachable"] = True
        info["device_count"] = len(devices)
        info["online_count"] = len(registry.online_devices)
        info["devices"] = [d.to_dict() for d in devices]
    except IMouseError as exc:
        info["error"] = str(exc)
    return info


@app.post("/api/debug/raw-command")
async def debug_raw_command(cmd: RawCommand):
    """Send an arbitrary ``fun`` to the API for testing."""
    try:
        result = await client.call(cmd.fun, **(cmd.data or {}))
        return {"success": True, "fun": cmd.fun, "data": result.data}
    except IMouseError as exc:
        return {"success": False, "fun": cmd.fun, "code": exc.code, "message": str(exc)}


# ------------------------------------------------------------------ config ----
@app.get("/api/config")
async def get_config():
    return {
        "imouse_host": config.imouse_host,
        "imouse_port": config.imouse_port,
        "anthropic_model": config.anthropic_model,
        "has_anthropic_key": bool(config.anthropic_api_key),
        "tiktok_ui": config.tiktok_ui,
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    if update.imouse_host:
        config.imouse_host = update.imouse_host
    if update.imouse_port:
        config.imouse_port = update.imouse_port
    if update.imouse_host or update.imouse_port:
        client.base_url = config.http_base_url
    if update.anthropic_api_key:
        config.anthropic_api_key = update.anthropic_api_key
        controller.ai.set_api_key(update.anthropic_api_key)
    if update.anthropic_model:
        config.anthropic_model = update.anthropic_model
    return {"status": "ok"}


@app.post("/api/config/ui")
async def update_ui_calibration(update: UICalibrationUpdate):
    entry = config.tiktok_ui.setdefault(update.element, {})
    entry["x"] = update.x
    entry["y"] = update.y
    if update.w is not None:
        entry["w"] = update.w
    if update.h is not None:
        entry["h"] = update.h
    return {"status": "ok", "element": update.element}


# ------------------------------------------------------------------ devices ---
@app.get("/api/devices")
async def get_devices():
    try:
        await registry.refresh()
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"devices": [d.to_dict() for d in registry.devices]}


@app.get("/api/devices/{device_id}/screenshot")
async def get_screenshot(device_id: str):
    try:
        return {"screenshot": await controller.get_screenshot_b64(device_id)}
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/devices/{device_id}/analyze")
async def analyze_video(device_id: str):
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    context = await controller.analyze_current_video(device_id)
    return {
        "caption": context.caption,
        "hashtags": context.hashtags,
        "username": context.username,
        "ocr_text": context.ocr_text,
        "has_screenshot": bool(context.screenshot_base64),
    }


# ------------------------------------------------------------------ actions ---
def _result(res) -> dict:
    return {"success": res.success, "message": res.message, **res.data}


@app.post("/api/devices/{device_id}/swipe/next")
async def swipe_next(device_id: str):
    return _result(await controller.swipe_next(device_id))


@app.post("/api/devices/{device_id}/swipe/previous")
async def swipe_previous(device_id: str):
    return _result(await controller.swipe_previous(device_id))


@app.post("/api/devices/{device_id}/like")
async def like_video(device_id: str, double_tap: bool = True):
    return _result(await controller.like_video(device_id, use_double_tap=double_tap))


@app.post("/api/devices/{device_id}/save")
async def save_video(device_id: str):
    return _result(await controller.save_video(device_id))


@app.post("/api/devices/{device_id}/follow")
async def follow_creator(device_id: str):
    return _result(await controller.follow_creator(device_id))


@app.post("/api/devices/{device_id}/comment")
async def comment_on_video(device_id: str, request: CommentRequest):
    res = await controller.comment_on_video(
        device_id,
        comment=request.comment,
        use_ai=request.use_ai,
        ai_style=request.ai_style,
    )
    return _result(res)


@app.post("/api/devices/{device_id}/generate-comments")
async def generate_comments(device_id: str, request: GenerateCommentRequest):
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    comments = await controller.generate_comment_options(device_id, count=request.count)
    return {"comments": comments}


# -------------------------------------------------------- tap test / debug ----
def _jpeg_size(data: bytes):
    """Return (width, height) of a JPEG by reading its SOF marker, or None."""
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xC3:
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h)
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:
            i += 2
            continue
        i += 2 + ((data[i + 2] << 8) | data[i + 3])
    return None


@app.post("/api/devices/{device_id}/tap")
async def tap(device_id: str, req: TapRequest):
    """Tap an exact pixel -- used by the frontend Tap Test to verify coordinates.

    Routed through the controller's settled click path (the same one the
    campaign uses) so the Tap Test measures real behaviour, including the
    pointer-settle that guards against relative-mouse drift.
    """
    result = await controller._click(device_id, req.x, req.y)
    return {"success": result.success, "x": req.x, "y": req.y, "message": result.message}


@app.get("/api/debug/screenshot-info")
async def debug_screenshot_info():
    """Same as screenshot-info but auto-picks the first online device (no ID to
    paste). Open http://localhost:8000/api/debug/screenshot-info directly."""
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    return await screenshot_info(online[0].device_id)


@app.get("/api/debug/ocr")
async def debug_ocr():
    """Check whether the PaddleOCR plugin is running, on the first online device.

    Open http://localhost:8000/api/debug/ocr directly in the browser.
    ``ocr_running: true`` with a non-zero ``elements`` count means OCR works and
    we can anchor taps to exact text coordinates. ``code: 28`` means the plugin
    process is not connected.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    device_id = online[0].device_id
    try:
        result = await controller.api.ocr(device_id)
    except IMouseError as exc:
        return {
            "ocr_running": False,
            "code": exc.code,
            "message": str(exc),
            "hint": "code 28 = PaddleOCR plugin not connected; start it in the console.",
        }
    items = result.items
    sample = [
        {"text": i.get("text"), "centre": i.get("centre") or i.get("center")}
        for i in items[:12]
    ]
    return {"ocr_running": True, "code": 0, "elements": len(items), "sample": sample}


@app.get("/api/debug/rail")
async def debug_rail():
    """Show exactly how the rail locator sees the current frame.

    Returns every right-edge OCR element (the candidate counts), which ones were
    treated as counts, and the icon points the locator computed. Open this on a
    frame where a tap missed to see why the like/comment mapping was off.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    device_id = online[0].device_id
    device = registry.get(device_id)
    w = (device.imgw or device.width) if device else 0
    h = (device.imgh or device.height) if device else 0

    perception = await agent.perceive(device_id)
    right_edge = [
        {"text": e["text"], "centre": e["centre"], "is_count": agent._is_count(e["text"])}
        for e in perception.get("elements", [])
        if e["centre"][0] > w * 0.85
    ]
    right_edge.sort(key=lambda e: e["centre"][1])
    points = agent._ocr_rail_points(perception, w, h)
    return {
        "imgw": w,
        "imgh": h,
        "right_edge_elements": right_edge,
        "computed_icon_points": {k: list(v) for k, v in points.items()},
        "locate_source_would_be": "ocr" if points.get("like_button") else "vision",
    }


@app.post("/api/mirror/calibrate")
async def mirror_calibrate():
    """Calibrate the AirPlay mirror re-cast: learn the Control Center Screen
    Mirroring button and the PC receiver row, so the campaign can re-establish a
    dropped mirror on its own. Run this ONCE while the mirror is up.

    It opens Control Center, vision-locates the mirroring button, taps it to
    reveal the receiver list (this does NOT disconnect), vision-locates the
    receiver (without tapping it), then closes. Returns the screenshots + points
    so you can verify. Saves to the 'control_center' UI map on success.
    """
    online = registry.online_devices
    if not online:
        raise HTTPException(status_code=404, detail="No online device")
    did = online[0].device_id
    device = registry.get(did)
    w = (device.imgw or device.width) if device else 0
    h = (device.imgh or device.height) if device else 0

    await client.send_key(did, fn_key="ControlBar")
    await asyncio.sleep(1.4)
    try:
        shot_cc = await controller.get_screenshot_b64(did)
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=f"screenshot failed: {exc}")
    mirror_pt = await agent.locate_one(
        shot_cc, w, h,
        "the Screen Mirroring / AirPlay button in iOS Control Center "
        "(icon of two overlapping rounded rectangles, often labelled "
        "'Bildschirmsynchronisation' or 'Screen Mirroring')",
    )

    shot_recv = None
    receiver_pt = None
    if mirror_pt:
        await client.click(did, mirror_pt[0], mirror_pt[1])
        await asyncio.sleep(1.6)
        try:
            shot_recv = await controller.get_screenshot_b64(did)
            receiver_pt = await agent.locate_one(
                shot_recv, w, h,
                "the FIRST device row in the AirPlay screen-mirroring list "
                "(the PC/Mac receiver to mirror to)",
            )
        except IMouseError:
            shot_recv = None

    await client.send_key(did, fn_key="WIN+h")  # Home closes Control Center
    await asyncio.sleep(1.0)

    elements = {}
    if mirror_pt:
        elements["screen_mirroring"] = list(mirror_pt)
    if receiver_pt:
        elements["airplay_receiver"] = list(receiver_pt)
    if elements:
        save_screen("control_center", elements, w, h)
    return {
        "ok": bool(mirror_pt and receiver_pt),
        "saved": elements,
        "screen_mirroring": mirror_pt,
        "airplay_receiver": receiver_pt,
        "screenshot_control_center": shot_cc,
        "screenshot_receivers": shot_recv,
        "hint": "Verify the points sit on the right controls. Re-run if off.",
    }


@app.get("/api/mirror/status")
async def mirror_status():
    """Report whether the mirror is up (a screenshot works) and whether re-cast
    is calibrated."""
    online = registry.online_devices
    calibrated = bool(load_map().get("screens", {}).get("control_center", {}).get("screen_mirroring"))
    if not online:
        return {"online": False, "mirror_up": False, "recast_calibrated": calibrated}
    did = online[0].device_id
    up = await controller.screenshot_works(did)
    return {"online": True, "mirror_up": up, "recast_calibrated": calibrated}


@app.get("/api/devices/{device_id}/screenshot-info")
async def screenshot_info(device_id: str):
    """Compare the screenshot's real JPEG size to the device's imgw/imgh tap
    space -- if they differ, mapped coordinates won't match taps."""
    device = registry.get(device_id)
    try:
        raw = await controller.get_screenshot_bytes(device_id)
    except IMouseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    size = _jpeg_size(raw)
    return {
        "jpeg_width": size[0] if size else None,
        "jpeg_height": size[1] if size else None,
        "imgw": device.imgw if device else None,
        "imgh": device.imgh if device else None,
        "match": bool(size and device and size == (device.imgw, device.imgh)),
    }


# ---------------------------------------------------- manual keyboard input ---
@app.post("/api/devices/{device_id}/type")
async def manual_type(device_id: str, request: ManualTypeRequest):
    """Type text into the phone's currently focused field, from the PC.

    Lets you type manually while the board stays connected, working around iOS
    hiding the on-screen keyboard when a hardware keyboard is attached. ASCII
    goes via /key/sendkey; non-ASCII via clipboard paste (with ASCII fallback).
    """
    return _result(await controller.enter_text(device_id, request.text))


@app.post("/api/devices/{device_id}/key")
async def manual_key(device_id: str, request: ManualKeyRequest):
    """Send a single key or hotkey (e.g. fn_key='ENTER' / 'BACKSPACE')."""
    if not request.key and not request.fn_key:
        raise HTTPException(status_code=400, detail="Provide 'key' or 'fn_key'")
    try:
        await client.send_key(device_id, key=request.key, fn_key=request.fn_key)
        return {"success": True, "message": "sent"}
    except IMouseError as exc:
        return {"success": False, "message": str(exc)}


# --------------------------------------------------------------- AI agent -----
@app.post("/api/devices/{device_id}/ai/step")
async def ai_step(device_id: str, request: AIAgentRequest):
    """Run one Claude perceive->decide->act cycle on the device."""
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    return await agent.step(device_id, goal=request.goal)


@app.post("/api/devices/{device_id}/ai/run")
async def ai_run(device_id: str, request: AIAgentRequest):
    """Run the Claude agent for up to max_steps toward a goal."""
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    return await agent.run(device_id, goal=request.goal, max_steps=request.max_steps)


# ------------------------------------------------------- interface explorer ---
@app.post("/api/devices/{device_id}/analyze-ui")
async def analyze_ui(device_id: str):
    """Map the current screen's controls to pixel coordinates (vision + OCR)."""
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    result = await agent.analyze_ui(device_id)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@app.get("/api/ui-map")
async def get_ui_map():
    return load_map()


@app.post("/api/ui-map")
async def post_ui_map(body: UIMapSave):
    return save_screen(body.screen_type, body.elements, body.imgw, body.imgh)


# ----------------------------------------------------------- niche campaign ---
@app.post("/api/campaign/start")
async def campaign_start(request: CampaignRequest):
    """Start a long-running, niche-aware campaign as a background task."""
    global campaign_task
    if not config.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")
    if agent.campaign_status()["running"]:
        raise HTTPException(status_code=409, detail="A campaign is already running")
    seconds = int(max(1.0, request.duration_minutes) * 60)
    campaign_task = asyncio.create_task(agent.campaign_loop(request.device_id, seconds))
    return {"status": "started", "duration_minutes": request.duration_minutes}


@app.post("/api/campaign/stop")
async def campaign_stop():
    """Ask the running campaign to stop at the next safe point."""
    agent.request_stop()
    return {"status": "stopping"}


@app.get("/api/campaign/status")
async def campaign_status():
    return agent.campaign_status()


# ------------------------------------------------------------------ batch -----
@app.post("/api/batch/action")
async def batch_action(request: BatchActionRequest):
    results = await controller.batch_action(request.device_ids, request.action)
    return {"results": {d: _result(r) for d, r in results.items()}}


@app.post("/api/devices/{device_id}/auto-browse")
async def auto_browse(device_id: str, request: AutoBrowseRequest):
    stats = await controller.auto_browse(
        device_id,
        video_count=request.video_count,
        like_probability=request.like_probability,
        comment_probability=request.comment_probability,
        min_watch_time=request.min_watch_time,
        max_watch_time=request.max_watch_time,
    )
    return {"stats": stats}


# --------------------------------------------------------- screenshot stream --
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Stream screenshots to the browser by polling ``/pic/screenshot``.

    Frames are sent as raw JPEG **binary** WebSocket messages -- no base64/JSON
    overhead (~25% smaller, no per-frame serialisation). The loop paces by the
    real fetch time rather than a fixed sleep, so it runs as fast as the console
    can supply frames up to the requested rate. The iMouse WebSocket is not used
    server-side (local HTTP is ~70 ms); this socket is browser <-> this server.
    """
    await websocket.accept()
    ws_clients.add(websocket)
    stream_task: Optional[asyncio.Task] = None

    async def stream(device_id: str, interval_ms: int):
        min_period = max(0.0, interval_ms / 1000)
        while websocket in ws_clients:
            start = time.monotonic()
            try:
                frame = await controller.get_screenshot_bytes(device_id)
                await websocket.send_bytes(frame)
            except Exception:  # noqa: BLE001 - stop streaming on any error
                break
            # Pace by how long the fetch actually took; no fixed dead time.
            await asyncio.sleep(max(0.0, min_period - (time.monotonic() - start)))

    def start_stream(data: dict) -> asyncio.Task:
        nonlocal stream_task
        if stream_task and not stream_task.done():
            stream_task.cancel()
        return asyncio.create_task(
            stream(data.get("device_id"), data.get("interval", 80))
        )

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "start_stream":
                stream_task = start_stream(data)
            elif action == "stop_stream" and stream_task:
                stream_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)
        if stream_task:
            stream_task.cancel()


# ---------------------------------------------------------------- frontend ----
frontend_path = Path(__file__).parent.parent / "frontend"


@app.get("/")
async def serve_frontend():
    return FileResponse(frontend_path / "index.html")


if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
