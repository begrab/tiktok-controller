# TikTok Accessibility Controller

A web-based controller for driving TikTok on a physical iPhone through the
**iMouse XP** phone-control setup. Taps and swipes travel over USB (the control
board presents as a HID keyboard/mouse to iOS); the phone screen is read back
over AirPlay mirroring. Optionally, **Claude** looks at the screen to understand
the current video and draft contextual comments.

No jailbreak, no app on the phone, no signing.

## How it fits together

```
Browser  ──HTTP/WS──►  this server (FastAPI, :8000)  ──HTTP──►  iMouse XP console (127.0.0.1:9911)
                                                                     │
                                              USB ── control board ── Lightning ── iPhone
                                                                     └── screen mirrored to PC (AirPlay)
```

Two independent channels to the phone:

- **Control** (taps/swipes/keys) over USB. Requires iOS accessibility settings
  (AssistiveTouch, full keyboard access, trackpad tracking at max).
- **Vision** (the screen image) over Wi-Fi via AirPlay. Phone and PC must be on
  the same subnet. If mirroring is down, screenshots and OCR fail even while
  control still works.

## Requirements

- Windows PC running the **iMouse XP** console (the local API lives inside it).
- iPhone 6s or newer, **iOS 13.4+**, connected via the board and mirrored to the PC.
- Python 3.10+.
- Optional: an **Anthropic API key** for AI comments/analysis.
- Optional: the **PaddleOCR plugin** running, for OCR / find-text.

## Key facts this controller is built on

- **API base:** `http://127.0.0.1:9911/api`. There is **no API key** — auth is
  being on localhost. Port **9911 is verified**; 9912 (from some third-party
  guides) is wrong.
- **Success = HTTP `status == 200` AND `data.code == 0`.** A 200 with a non-zero
  `data.code` is a failure.
- **Coordinate space:** all taps and OCR centres are in `imgw × imgh` (the
  mirror-stream resolution, e.g. 608×1080) — **read at runtime per device**,
  never the iOS logical points (`width`/`height`) and never hardcoded.
- **Screenshots** with `binary:true` return raw JPEG bytes, not JSON.
- **Non-ASCII text** (emoji/accents): set the clipboard, focus the field, then
  paste with `WIN+v` — `/key/sendkey` is ASCII only.
- **OCR** returns code **28** until the PaddleOCR plugin process is running;
  those calls are skipped gracefully.

## Installation

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Defaults live at the top of `backend/config.py`. Override via environment
variables (copy `backend/.env.example` to `backend/.env`):

```
IMOUSE_HOST=127.0.0.1
IMOUSE_PORT=9911
ANTHROPIC_API_KEY=sk-ant-...          # only for AI features
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

Models available (in/out $ per Mtok): Haiku 4.5 `$1/$5`, Sonnet 4.6 `$3/$15`,
Opus 4.8 `$5/$25`. A 608×1080 screenshot is ~875 input tokens.

## Usage

- Windows: `run.bat`
- macOS/Linux: `./run.sh`

Then open <http://localhost:8000>. Use `GET /api/debug/connection` to confirm the
console is reachable and see discovered devices.

## API endpoints (this server)

- `GET  /api/devices` — online/known devices (with `imgw`/`imgh`)
- `GET  /api/devices/{id}/screenshot` — base64 JPEG
- `GET  /api/devices/{id}/analyze` — Claude + OCR video context
- `POST /api/devices/{id}/swipe/next` · `/swipe/previous`
- `POST /api/devices/{id}/like` · `/save` · `/follow`
- `POST /api/devices/{id}/comment` — type/post a comment (AI optional)
- `POST /api/devices/{id}/generate-comments` — draft options
- `POST /api/devices/{id}/ai/step` — one Claude perceive→decide→act cycle
- `POST /api/devices/{id}/ai/run` — autonomous agent loop (`goal`, `max_steps`)
- `POST /api/batch/action` — one action across many devices
- `POST /api/devices/{id}/auto-browse` — randomised engagement session
- `GET  /api/debug/connection` · `POST /api/debug/raw-command`

## Architecture

- `backend/api_client.py` — verified iMouse XP HTTP API (`IMouseClient`).
- `backend/device_manager.py` — `/device/get` discovery + the `imgw/imgh`
  coordinate mapping (`Device`, `DeviceRegistry`).
- `backend/ai_comments.py` — Claude vision analysis and comment generation.
- `backend/tiktok_actions.py` — high-level TikTok flows (`TikTokController`).
- `backend/ai_agent.py` — Claude perceive→decide→act loop (`TikTokAgent`): OCR
  grounds tappable text, Claude picks one action via tool-use, backend executes.
- `backend/main.py` — FastAPI server + screenshot streaming to the browser.
- `frontend/` — HTML/JS control panel.

`tiktok-controller.html` at the repo root is a standalone single-file prototype
kept for reference; the maintained app is the `backend/` + `frontend/` pair.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No devices | console not running, phone not mirrored, or on another subnet |
| Screenshot fails, control works | AirPlay mirroring dropped — re-cast from the phone |
| Taps land in the wrong place | wrong coordinate space, or mouse params set to the `<=14.2` variant |
| OCR / find-text returns code 28 | the PaddleOCR console window was closed |
| AI features rejected (400) | `ANTHROPIC_API_KEY` not set |

## License

MIT
