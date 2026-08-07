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
    # Comments are rare and matter, so write them with a stronger model than the
    # fast per-frame classifier. Override with ANTHROPIC_COMMENT_MODEL.
    comment_model: str = os.getenv("ANTHROPIC_COMMENT_MODEL", "claude-sonnet-4-6")

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
    action_delay: float = 0.3      # settle time between chained taps
    paste_delay: float = 0.25      # focus -> paste settle time
    swipe_length: float = 0.8      # fraction of screen per swipe -- a firm flick;
                                   # too short and TikTok snaps back to the same video

    # Relative-mouse pointer settling. The board is a RELATIVE HID mouse: the
    # console turns an absolute (x, y) into a move-from-current-position then a
    # click. If the pointer is still travelling (tracking speed) or was asleep
    # (AssistiveTouch auto-hide), the click fires early and lands short. These
    # let the pointer come to rest before we tap. The real fix is phone/console
    # settings (tracking speed at max, no auto-hide) -- these just stop us from
    # racing the pointer between our own commands.
    pre_tap_settle: float = 0.1    # pause before every tap so the pointer is at rest
    post_swipe_settle: float = 0.6   # rest after a swipe: pointer settles AND the
                                     # laggy mirror catches up to the new video

    # Right-rail icon targeting. Each rail icon (heart/comment/bookmark/share)
    # sits directly ABOVE its count number, which OCR locates to the exact pixel.
    # The icon is this fraction of the count-to-count spacing above its count; if
    # spacing can't be measured (one count only), fall back to the absolute px.
    rail_icon_offset_factor: float = 0.42
    rail_icon_offset_px: int = 48

    # Niche campaign behaviour. Engagement is randomised per relevant video, so
    # combinations vary naturally; watch times are jittered with occasional long
    # tails so the pacing reads as human, not on a fixed timer.
    campaign: dict = field(
        default_factory=lambda: {
            # Independent per-action chances on a RELEVANT video.
            "like_probability": 0.85,
            "save_probability": 0.5,
            # Commenting is deliberately SPARSE so it never looks spammy: a
            # comment is only considered once the cooldown has passed (this many
            # relevant videos since the last comment, randomized), then fires with
            # this probability, and never more than the per-run cap. Comments are
            # always unique, personal-struggle stories (see niche.comment_style).
            "comment_probability": 0.7,       # chance WHEN eligible (cooldown passed)
            "comment_min_gap": [6, 12],        # relevant videos between comments
            "comment_max_per_session": 8,      # hard cap per campaign run
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
            # Photo slideshows: browse some slides (all when commenting). Swipe
            # direction to advance a slide -- flip to "left" if slides go backward.
            "slideshow_swipe_direction": "right",
            "slide_dwell": [1.5, 4.0],       # pause on each slide
            "slideshow_max_slides": 8,       # cap when the total is unknown
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
