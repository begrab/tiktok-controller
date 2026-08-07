"""Persistent per-run campaign logs.

Each warm-up/campaign run is saved to ``backend/campaign_logs/<session>/`` with:
  * ``meta.json``     -- device, niche, start time
  * ``log.jsonl``     -- one line per cycle (action, screen, relevance, reason,
                         comment, errors, timestamp) + the frame filename
  * ``frames/NNNN.jpg`` -- the screenshot the decision was made on
  * ``summary.json``  -- stats + counts, written when the run ends

This is the raw material for reviewing runs and improving the swiping/engagement
logic over time. It's runtime data (gitignored).
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime

LOG_ROOT = os.path.join(os.path.dirname(__file__), "campaign_logs")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", (s or ""))[:40]


class SessionLogger:
    """Writes one campaign run to its own folder."""

    def __init__(self, device_id: str, niche_name: str):
        self.started = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.name = f"{self.started}_{_safe(niche_name) or 'run'}_{_safe(device_id)[-6:]}"
        self.dir = os.path.join(LOG_ROOT, self.name)
        self.frames_dir = os.path.join(self.dir, "frames")
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self.count = 0
        try:
            os.makedirs(self.frames_dir, exist_ok=True)
            with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"started": self.started, "device": device_id,
                           "niche": niche_name}, f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - logging must never break the run
            pass

    def log(self, entry: dict, screenshot_b64: str | None = None) -> None:
        self.count += 1
        rec: dict = {"i": self.count}
        rec.update(entry or {})
        if screenshot_b64:
            try:
                fn = f"{self.count:04d}.jpg"
                with open(os.path.join(self.frames_dir, fn), "wb") as f:
                    f.write(base64.b64decode(screenshot_b64))
                rec["frame"] = f"frames/{fn}"
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def finish(self, stats: dict) -> None:
        try:
            with open(os.path.join(self.dir, "summary.json"), "w", encoding="utf-8") as f:
                json.dump({"started": self.started,
                           "ended": datetime.now().strftime("%Y%m%d-%H%M%S"),
                           "entries": self.count, "stats": stats},
                          f, indent=2, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass


def read_session(name: str) -> dict | None:
    """Return one saved run's meta + per-cycle entries (for the review gallery)."""
    name = _safe(name)
    d = os.path.join(LOG_ROOT, name)
    if not os.path.isdir(d):
        return None
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    entries = []
    lp = os.path.join(d, "log.jsonl")
    if os.path.exists(lp):
        try:
            with open(lp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    summary = {}
    sp = os.path.join(d, "summary.json")
    if os.path.exists(sp):
        try:
            summary = json.load(open(sp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"name": name, "meta": meta, "summary": summary, "entries": entries}


def list_sessions(limit: int = 60) -> list:
    """Newest-first list of saved runs with their summary/stats."""
    if not os.path.isdir(LOG_ROOT):
        return []
    out = []
    for name in sorted(os.listdir(LOG_ROOT), reverse=True)[:limit]:
        d = os.path.join(LOG_ROOT, name)
        if not os.path.isdir(d):
            continue
        info = {"name": name, "entries": 0, "stats": {}, "running": True}
        sp = os.path.join(d, "summary.json")
        if os.path.exists(sp):
            try:
                info.update(json.load(open(sp, encoding="utf-8")))
                info["running"] = False
            except Exception:  # noqa: BLE001
                pass
        else:
            lp = os.path.join(d, "log.jsonl")
            if os.path.exists(lp):
                try:
                    info["entries"] = sum(1 for _ in open(lp, encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    pass
        out.append(info)
    return out
