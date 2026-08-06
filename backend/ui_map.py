"""Persistent UI map -- the "learning doc" of TikTok's interface on THIS phone.

Built interactively via the Interface Explorer: for each screen type we store
the pixel tap point of each named control (in the device's imgw x imgh space).
Navigation reads from this map instead of hardcoded fractional guesses.

Structure (ui_map.json):
    {
      "imgw": 608, "imgh": 1080,
      "screens": {
        "for_you_feed": {"like_button": [560, 470], "comment_button": [560, 560], ...},
        "search_results": {"back_arrow": [30, 60], ...},
        ...
      }
    }
"""
from __future__ import annotations

import json
import os
from typing import Optional

_PATH = os.path.join(os.path.dirname(__file__), "ui_map.json")


def load_map() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("screens", {})
    return data


def save_screen(screen_type: str, elements: dict, imgw: int = 0, imgh: int = 0) -> dict:
    """Store/replace the control points for one screen type. ``elements`` is
    ``{label: [x, y]}``."""
    data = load_map()
    if imgw:
        data["imgw"] = imgw
    if imgh:
        data["imgh"] = imgh
    cleaned = {}
    for label, xy in (elements or {}).items():
        if isinstance(xy, (list, tuple)) and len(xy) == 2:
            cleaned[str(label)] = [int(xy[0]), int(xy[1])]
    data["screens"][screen_type] = cleaned
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def get_point(screen_type: str, label: str) -> Optional[list]:
    """Return [x, y] for a mapped control, or None if not mapped yet."""
    pt = load_map()["screens"].get(screen_type, {}).get(label)
    return pt if isinstance(pt, list) and len(pt) == 2 else None
