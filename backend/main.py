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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

config = Config.from_env()
client: Optional[IMouseClient] = None
registry: Optional[DeviceRegistry] = None
controller: Optional[TikTokController] = None
agent: Optional[TikTokAgent] = None
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
