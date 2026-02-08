# TikTok Accessibility Controller

A web-based controller for TikTok accessibility, allowing automated interactions via iMouse/PhoneFarm software.

## Features

- **Device Management**: Connect and control multiple iOS devices via iMouseXP/PhoneFarm
- **TikTok Actions**: Swipe, like, save, follow, and comment on videos
- **AI Comments**: Generate contextual comments using OpenAI GPT-4
- **Batch Operations**: Control multiple devices simultaneously
- **Real-time Screenshots**: Live device screen streaming via WebSocket
- **Web Interface**: Control devices from any browser

## Requirements

- Python 3.10+
- iMouseXP or PhoneFarm software running on Windows
- iOS devices connected via USB

## Installation

### Backend Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

Edit `backend/config.py` or set environment variables:

```python
IMOUSE_HOST = "127.0.0.1"      # iMouse server address
IMOUSE_HTTP_PORT = 9911        # iMouse API port
IMOUSE_WS_PORT = 9911          # iMouse WebSocket port
```

Optional: Set `OPENAI_API_KEY` environment variable for AI-generated comments.

## Usage

### On Windows

```bash
run.bat
```

### On macOS/Linux

```bash
./run.sh
```

Then open http://localhost:8000 in your browser.

## API Endpoints

- `GET /api/devices` - List connected devices
- `POST /api/devices/{device_id}/swipe/next` - Swipe to next video
- `POST /api/devices/{device_id}/like` - Like current video
- `POST /api/devices/{device_id}/comment` - Comment on video
- `GET /api/devices/{device_id}/screenshot` - Get device screenshot
- `GET /api/debug/connection` - Debug connection status

## Architecture

- **FastAPI Backend**: Python async server handling device communication
- **WebSocket Device Discovery**: Real-time device connect/disconnect events
- **HTTP + WebSocket Control**: Automatic fallback between protocols
- **Frontend**: Simple HTML/JS interface for device control

## License

MIT
