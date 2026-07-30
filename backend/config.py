"""Configuration for the TikTok controller.

Defaults match the verified iMouse XP setup: the console's local API is on
``127.0.0.1:9911`` (port 9912, seen in some third-party guides, is wrong) and
there is no API key -- auth is being on localhost.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Optional: load a .env file if python-dotenv is installed.
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:  # python-dotenv not installed -- rely on real env vars.
    pass


# =============================================================================
# EDIT THESE FOR YOUR SETUP
# =============================================================================
IMOUSE_HOST = "127.0.0.1"   # iMouse console (localhost if on the same machine)
IMOUSE_PORT = 9911          # verified iMouse XP API port -- do NOT use 9912
# =============================================================================

# Default Claude model. Cheap and fast for per-screenshot calls; the handover
# lists claude-sonnet-4-6 and claude-opus-4-8 as more capable alternatives.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class Config:
    # iMouse XP local API.
    imouse_host: str = os.getenv("IMOUSE_HOST", IMOUSE_HOST)
    imouse_port: int = int(os.getenv("IMOUSE_PORT", IMOUSE_PORT))

    # Anthropic (only needed when an LLM is in the loop). Read from the
    # environment; never hardcode a key.
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)

    # OCR / find-text tuning. The bundled model is tuned for Chinese and
    # English; lower this for accented or stylised Latin text.
    ocr_similarity: float = 0.8

    # Approximate TikTok UI anchors as FRACTIONS of the imgw x imgh coordinate
    # space. Icons have no text label, so find-text can't locate them -- these
    # are the fallback and can be calibrated per device from the UI. Anything
    # with a text label (Follow, Post) is located with find-text instead.
    tiktok_ui: dict = field(
        default_factory=lambda: {
            "like_button": {"x": 0.92, "y": 0.55},
            "comment_button": {"x": 0.92, "y": 0.66},
            "share_button": {"x": 0.92, "y": 0.77},
            "video_center": {"x": 0.5, "y": 0.5},
            "comment_input": {"x": 0.5, "y": 0.95},
            "caption_area": {"x": 0.03, "y": 0.72, "w": 0.75, "h": 0.18},
            # Recovery anchors. back_arrow is the universal top-left "go back"
            # button on search/profile/detail screens; home_tab is the Home tab
            # on the bottom bar. Calibrate if taps miss.
            "back_arrow": {"x": 0.05, "y": 0.055},
            "home_tab": {"x": 0.1, "y": 0.97},
        }
    )

    # Timing (seconds).
    action_delay: float = 0.5      # settle time between chained taps
    paste_delay: float = 0.3       # focus -> paste settle time
    swipe_length: float = 0.6      # fraction of the screen per swipe

    # Niche campaign behaviour. Engagement is randomised per relevant video, so
    # combinations vary naturally; watch times are jittered with occasional long
    # tails so the pacing reads as human, not on a fixed timer.
    campaign: dict = field(
        default_factory=lambda: {
            # Independent per-action chances on a RELEVANT video.
            "like_probability": 0.85,
            "save_probability": 0.5,
            "comment_probability": 0.4,
            # Watch time (seconds) ranges: [min, max]; a "long" tail fires with
            # its chance for the occasional deep-watch / longer glance.
            "watch_relevant": [8.0, 32.0],
            "watch_relevant_long": [32.0, 70.0],
            "watch_relevant_long_chance": 0.15,
            "watch_irrelevant": [1.0, 4.0],
            "watch_irrelevant_long": [4.0, 7.0],
            "watch_irrelevant_long_chance": 0.10,
            # Small human gap between chained engagement actions.
            "between_actions": [0.6, 2.2],
        }
    )

    @property
    def http_base_url(self) -> str:
        return f"http://{self.imouse_host}:{self.imouse_port}/api"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            imouse_host=os.getenv("IMOUSE_HOST", IMOUSE_HOST),
            imouse_port=int(os.getenv("IMOUSE_PORT", IMOUSE_PORT)),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        )
