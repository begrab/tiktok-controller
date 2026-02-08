"""FastAPI server for TikTok Controller web interface."""
import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import Config
from tiktok_actions import TikTokController
from device_manager import DeviceManager

logger = logging.getLogger("main")

# Global state
config = Config.from_env()
controller: Optional[TikTokController] = None
device_manager: Optional[DeviceManager] = None
ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global controller, device_manager

    # Log actual config values for debugging
    logger.info(f"Config: host={config.imouse_host}, http_port={config.imouse_http_port}, ws_port={config.imouse_ws_port}")

    # iMouseXP uses the SAME port (9911) for both HTTP and WebSocket
    # Use http_port for both to ensure consistency
    ws_port = config.imouse_http_port  # Same port for iMouseXP

    # Initialize device manager (WebSocket-based device discovery)
    device_manager = DeviceManager(
        host=config.imouse_host,
        port=ws_port
    )

    # Start WebSocket connection for device discovery
    await device_manager.start()

    # Initialize controller (HTTP-based commands)
    controller = TikTokController(config, device_manager)
    await controller.api.__aenter__()

    logger.info("Application started")

    yield

    # Cleanup
    await controller.api.__aexit__(None, None, None)
    await device_manager.stop()
    logger.info("Application stopped")


app = FastAPI(
    title="TikTok Accessibility Controller",
    description="Control TikTok on your devices via iMouse/PhoneFarm",
    version="1.0.0",
    lifespan=lifespan
)

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request/Response Models
class ConfigUpdate(BaseModel):
    imouse_host: Optional[str] = None
    imouse_http_port: Optional[int] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None


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


# API Routes
@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/debug/connection")
async def debug_connection():
    """Debug endpoint to test iMouse connection."""
    http_result = await controller.api.test_connection()
    ws_status = device_manager.get_status()

    return {
        "imouse_config": {
            "host": config.imouse_host,
            "configured_http_port": config.imouse_http_port,
            "configured_ws_port": config.imouse_ws_port,
            "actual_http_url": controller.api.base_url,
            "actual_ws_url": device_manager.ws_url,
        },
        "http_api": {
            "working_port": controller.api._working_port,
            **http_result
        },
        "websocket": ws_status
    }


@app.post("/api/debug/raw-command")
async def debug_raw_command(fun: str, data: dict = None):
    """Send a raw command to iMouse API for testing."""
    response = await controller.api._request(fun, data or {})
    return {
        "fun": fun,
        "success": response.success,
        "status": response.status,
        "message": response.message,
        "data": response.data
    }


@app.post("/api/debug/test-swipe")
async def debug_test_swipe(device_id: str = ""):
    """Test swipe command - uses provided device_id or tries to find one."""
    # If no device_id provided, try to get one from the device list
    if not device_id:
        devices = await controller.refresh_devices()
        if devices:
            device_id = devices[0].device_id
        else:
            # Try with empty device_id - some iMouse versions work this way
            device_id = ""

    # Try swipe
    response = await controller.api.swipe(device_id, direction="up", length=0.5)
    return {
        "device_id_used": device_id,
        "success": response.success,
        "status": response.status,
        "message": response.message
    }


@app.get("/api/config")
async def get_config():
    """Get current configuration (excluding sensitive data)."""
    return {
        "imouse_host": config.imouse_host,
        "imouse_http_port": config.imouse_http_port,
        "imouse_ws_port": config.imouse_ws_port,
        "openai_model": config.openai_model,
        "has_openai_key": bool(config.openai_api_key),
        "tiktok_ui": config.tiktok_ui
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    """Update configuration."""
    if update.imouse_host:
        config.imouse_host = update.imouse_host
    if update.imouse_http_port:
        config.imouse_http_port = update.imouse_http_port
    if update.openai_api_key:
        config.openai_api_key = update.openai_api_key
        controller.ai.set_api_key(update.openai_api_key)
    if update.openai_model:
        config.openai_model = update.openai_model
    return {"status": "ok"}


@app.post("/api/config/ui")
async def update_ui_calibration(update: UICalibrationUpdate):
    """Update UI element coordinates."""
    if update.element not in config.tiktok_ui:
        config.tiktok_ui[update.element] = {}

    config.tiktok_ui[update.element]["x"] = update.x
    config.tiktok_ui[update.element]["y"] = update.y
    if update.w is not None:
        config.tiktok_ui[update.element]["w"] = update.w
    if update.h is not None:
        config.tiktok_ui[update.element]["h"] = update.h

    return {"status": "ok", "element": update.element}


# Device Management
@app.get("/api/devices")
async def get_devices():
    """Get list of all devices from WebSocket-based registry."""
    # Get devices from WebSocket-backed device manager
    devices = device_manager.devices

    # If no devices and WebSocket is connected, try refreshing
    if not devices and device_manager.is_connected:
        devices = await device_manager.refresh_devices()

    return {
        "devices": [d.to_dict() for d in devices],
        "source": "websocket",
        "ws_connected": device_manager.is_connected
    }


@app.get("/api/devices/{device_id}/screenshot")
async def get_screenshot(device_id: str):
    """Get screenshot from device."""
    screenshot = await controller.get_screenshot(device_id)
    if screenshot:
        return {"screenshot": screenshot}
    raise HTTPException(status_code=500, detail="Failed to get screenshot")


@app.get("/api/devices/{device_id}/analyze")
async def analyze_video(device_id: str):
    """Analyze current video on device."""
    if not config.openai_api_key:
        raise HTTPException(status_code=400, detail="OpenAI API key not configured")

    context = await controller.analyze_current_video(device_id)
    return {
        "caption": context.caption,
        "hashtags": context.hashtags,
        "username": context.username,
        "ocr_text": context.ocr_text,
        "has_screenshot": bool(context.screenshot_base64)
    }


# TikTok Actions
@app.post("/api/devices/{device_id}/swipe/next")
async def swipe_next(device_id: str):
    """Swipe to next video."""
    response = await controller.swipe_next(device_id)
    return {"success": response.success, "message": response.message}


@app.post("/api/devices/{device_id}/swipe/previous")
async def swipe_previous(device_id: str):
    """Swipe to previous video."""
    response = await controller.swipe_previous(device_id)
    return {"success": response.success, "message": response.message}


@app.post("/api/devices/{device_id}/like")
async def like_video(device_id: str, double_tap: bool = True):
    """Like the current video."""
    response = await controller.like_video(device_id, use_double_tap=double_tap)
    return {"success": response.success, "message": response.message}


@app.post("/api/devices/{device_id}/save")
async def save_video(device_id: str):
    """Save/bookmark the current video."""
    response = await controller.save_video(device_id)
    return {"success": response.success, "message": response.message}


@app.post("/api/devices/{device_id}/follow")
async def follow_creator(device_id: str):
    """Follow the current video's creator."""
    response = await controller.follow_creator(device_id)
    return {"success": response.success, "message": response.message}


@app.post("/api/devices/{device_id}/comment")
async def comment_on_video(request: CommentRequest):
    """Comment on the current video."""
    result = await controller.comment_on_video(
        request.device_id,
        comment=request.comment,
        use_ai=request.use_ai,
        ai_style=request.ai_style
    )
    return result


@app.post("/api/devices/{device_id}/generate-comments")
async def generate_comments(device_id: str, request: GenerateCommentRequest):
    """Generate AI comment options for current video."""
    if not config.openai_api_key:
        raise HTTPException(status_code=400, detail="OpenAI API key not configured")

    comments = await controller.generate_comment_options(
        device_id,
        count=request.count
    )
    return {"comments": comments}


# Batch Operations
@app.post("/api/batch/action")
async def batch_action(request: BatchActionRequest):
    """Perform action on multiple devices."""
    results = await controller.batch_action(request.device_ids, request.action)
    return {
        "results": {
            device_id: {"success": r.success, "message": r.message}
            for device_id, r in results.items()
        }
    }


@app.post("/api/batch/swipe-next")
async def batch_swipe_next(device_ids: list[str]):
    """Swipe next on multiple devices."""
    results = await controller.batch_action(device_ids, "swipe_next")
    return {
        "results": {
            device_id: {"success": r.success, "message": r.message}
            for device_id, r in results.items()
        }
    }


# Automation
@app.post("/api/devices/{device_id}/auto-browse")
async def auto_browse(device_id: str, request: AutoBrowseRequest):
    """Start automated browsing session."""
    stats = await controller.auto_browse(
        device_id,
        video_count=request.video_count,
        like_probability=request.like_probability,
        comment_probability=request.comment_probability,
        min_watch_time=request.min_watch_time,
        max_watch_time=request.max_watch_time
    )
    return {"stats": stats}


# WebSocket for real-time updates
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time screenshot streaming and updates."""
    await websocket.accept()
    ws_clients.add(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "start_stream":
                device_id = data.get("device_id")
                interval = data.get("interval", 200)

                # Start streaming screenshots
                async def stream_screenshots():
                    while websocket in ws_clients:
                        try:
                            screenshot = await controller.get_screenshot(device_id)
                            if screenshot:
                                await websocket.send_json({
                                    "type": "screenshot",
                                    "device_id": device_id,
                                    "data": screenshot
                                })
                            await asyncio.sleep(interval / 1000)
                        except Exception:
                            break

                asyncio.create_task(stream_screenshots())

            elif action == "stop_stream":
                pass  # Handled by removing from ws_clients

    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)


# Serve frontend
frontend_path = Path(__file__).parent.parent / "frontend"


@app.get("/")
async def serve_frontend():
    """Serve the frontend."""
    return FileResponse(frontend_path / "index.html")


# Mount static files
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
