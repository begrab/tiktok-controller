"""Configuration settings for TikTok Controller."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Optional: Load .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass  # python-dotenv not installed, use defaults


# =============================================================================
# EDIT THESE VALUES FOR YOUR SETUP
# =============================================================================
IMOUSE_HOST = "127.0.0.1"      # iMouse server (localhost if same machine)
IMOUSE_HTTP_PORT = 9911        # iMouse API port (9911 for iMouseXP)
IMOUSE_WS_PORT = 9911          # iMouse WebSocket port (same as HTTP for iMouseXP)
# =============================================================================


@dataclass
class Config:
    # iMouse/PhoneFarm server settings (uses values above, or env override)
    imouse_host: str = os.getenv("IMOUSE_HOST", IMOUSE_HOST)
    imouse_http_port: int = int(os.getenv("IMOUSE_HTTP_PORT", IMOUSE_HTTP_PORT))
    imouse_ws_port: int = int(os.getenv("IMOUSE_WS_PORT", IMOUSE_WS_PORT))

    # OpenAI settings
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    # TikTok UI coordinates (percentage of screen, calibrate as needed)
    # These are approximate and may need adjustment per device
    tiktok_ui: dict = field(default_factory=lambda: {
        "like_button": {"x": 0.92, "y": 0.45},      # Heart icon on right
        "comment_button": {"x": 0.92, "y": 0.55},   # Comment icon
        "share_button": {"x": 0.92, "y": 0.65},     # Share icon
        "save_button": {"x": 0.5, "y": 0.85},       # Save in share menu
        "follow_button": {"x": 0.92, "y": 0.35},    # Follow button
        "video_center": {"x": 0.5, "y": 0.5},       # Center of video
        "caption_area": {"x": 0.05, "y": 0.75, "w": 0.7, "h": 0.15},  # Caption/hashtags
        "comment_input": {"x": 0.5, "y": 0.95},     # Comment text field
        "post_button": {"x": 0.5, "y": 0.95},       # Post comment button
        "create_button": {"x": 0.5, "y": 0.95},     # Bottom center + button
    })

    # Timing settings (milliseconds)
    click_duration: int = 50
    double_tap_interval: int = 100
    swipe_length: float = 0.5  # 50% of screen
    action_delay: int = 500    # Delay between actions

    @property
    def http_base_url(self) -> str:
        return f"http://{self.imouse_host}:{self.imouse_http_port}/api"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.imouse_host}:{self.imouse_ws_port}/client"

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration. Uses hardcoded defaults from top of file."""
        return cls(
            imouse_host=os.getenv("IMOUSE_HOST", IMOUSE_HOST),
            imouse_http_port=int(os.getenv("IMOUSE_HTTP_PORT", IMOUSE_HTTP_PORT)),
            imouse_ws_port=int(os.getenv("IMOUSE_WS_PORT", IMOUSE_WS_PORT)),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        )
