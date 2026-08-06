"""Self-verifying button test harness.

Runs on the machine the phone is connected to (the console must be running).
For each of many feed videos it taps a button, then decides ON ITS OWN whether
the tap hit the right control -- no human watching:

  * like    -> the heart pixel flips white<->red (TikTok like colour)
  * save    -> the bookmark pixel flips white<->yellow
  * comment -> the comment sheet actually opens (OCR + big frame change)
  * share   -> the share sheet actually opens (OCR + big frame change)

A full-frame diff separates three outcomes: HIT (tiny change + colour toggle),
MISSED (tiny change, no toggle -- tapped dead space) and NAVIGATED (huge change
-- tapped a nav control like the profile; we then recover to the feed). Toggle
tests restore state (un-like / un-save) so the run leaves no trace, and sheet
tests are dismissed without posting.

Every failure captures the exact rail OCR data and computed points, so the
report says not just THAT a tap missed but WHY.

Usage (from backend/, with the venv active or via selftest.bat):
    python selftest.py [rounds] [buttons]
    python selftest.py 20 like,comment
Defaults: 15 rounds, buttons = like,save,comment,share.
Writes selftest_report.json and prints a summary.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import sys
from typing import Optional

from ai_agent import TikTokAgent
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import DeviceRegistry
from tiktok_actions import TikTokController

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


# ----------------------------------------------------------------- imaging ---
def _load(perc: dict):
    b64 = perc.get("screenshot_b64")
    if not (b64 and Image):
        return None
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _avg(img, x: int, y: int, r: int = 5):
    W, H = img.size
    box = (max(0, x - r), max(0, y - r), min(W, x + r + 1), min(H, y + r + 1))
    px = list(img.crop(box).getdata())
    n = max(1, len(px))
    return (sum(p[0] for p in px) / n, sum(p[1] for p in px) / n, sum(p[2] for p in px) / n)


def _redness(img, x: int, y: int) -> float:
    """High for TikTok's red heart (~254,44,85); ~0 for a white outline."""
    R, G, B = _avg(img, x, y)
    return R - (G + B) / 2.0


def _yellowness(img, x: int, y: int) -> float:
    """High for the yellow saved-bookmark; ~0 for a white outline."""
    R, G, B = _avg(img, x, y)
    return (R + G) / 2.0 - B


def _frame_diff(a, b) -> float:
    """Mean absolute grayscale diff 0..1 of downscaled frames.

    A like/save recolours a tiny icon (tiny diff). Opening a sheet or navigating
    away changes most of the screen (large diff).
    """
    if not (a and b):
        return 0.0
    a = a.resize((48, 48)).convert("L")
    b = b.resize((48, 48)).convert("L")
    da, db = list(a.getdata()), list(b.getdata())
    return sum(abs(x - y) for x, y in zip(da, db)) / (len(da) * 255.0)


# --------------------------------------------------------------- semantics ---
COMMENT_HINTS = ("kommentar", "comment", "antworten", "reply")
SHARE_HINTS = ("teilen", "share", "repost", "whatsapp", "link kopieren", "copy link", "senden an")
OFFFEED_HINTS = {
    "profile": ("follower", "gefolgt", "abonniert", "following", "beiträge", "posts", "reposts"),
    "comments": ("kommentar", "comment"),
    "share_sheet": ("teilen", "whatsapp", "link kopieren", "repost"),
    "live": ("live", "zuschauer", "viewers"),
    "search": ("suchen", "search", "ergebnisse", "results"),
}


def _texts(perc: dict) -> list[str]:
    return [(e.get("text") or "").lower() for e in perc.get("elements", [])]


def _has_any(perc: dict, hints) -> bool:
    joined = " ".join(_texts(perc))
    return any(h in joined for h in hints)


def _guess_screen(perc: dict) -> str:
    for name, hints in OFFFEED_HINTS.items():
        if _has_any(perc, hints):
            return name
    return "unknown"


def _scenario(perc: dict, w: int, h: int) -> dict:
    elems = perc.get("elements", [])
    texts = [e.get("text", "") for e in elems]
    slideshow = any(re.search(r"\b\d+\s*/\s*\d+\b", t) for t in texts)
    bottom = [
        {"text": e["text"], "centre": e["centre"]}
        for e in elems if e["centre"][1] > h * 0.86
    ]
    rail = [e for e in elems if e["centre"][0] > w * 0.85 and h * 0.12 < e["centre"][1] < h * 0.9]
    label = "slideshow" if slideshow else ("search_bar" if bottom else "plain_video")
    return {
        "label": label,
        "slideshow": slideshow,
        "bottom_bar_elements": bottom,
        "n_rail_counts": len(rail),
    }


def _right_edge(perc: dict, w: int) -> list[dict]:
    rows = [
        {"text": e["text"], "centre": e["centre"]}
        for e in perc.get("elements", []) if e["centre"][0] > w * 0.85
    ]
    rows.sort(key=lambda e: e["centre"][1])
    return rows


# ------------------------------------------------------------- harness core --
class SelfTest:
    def __init__(self, controller: TikTokController, agent: TikTokAgent, device):
        self.c = controller
        self.agent = agent
        self.dev = device
        self.did = device.device_id
        self.w = device.imgw or device.width
        self.h = device.imgh or device.height

    async def _perc_img(self):
        perc = await self.agent.perceive(self.did)
        return perc, _load(perc)

    async def _tap(self, x: int, y: int):
        await self.c._click(self.did, int(x), int(y))

    async def _recover(self) -> str:
        """Get back to a feed video after a mis-tap navigated away."""
        try:
            await self.c.api.send_key(self.did, fn_key="TAB+b")
        except IMouseError:
            pass
        await asyncio.sleep(0.8)
        perc, _ = await self._perc_img()
        if _guess_screen(perc) != "unknown":
            # still off-feed: pull down (dismiss sheet) then Home
            try:
                await self.c.api.swipe(self.did, "down", 0.6)
            except IMouseError:
                pass
            await asyncio.sleep(0.8)
        return _guess_screen(perc)

    async def _toggle_test(self, name: str, point, metric_fn, img_before, on_thresh: float):
        """Shared logic for like/save: tap, classify, restore."""
        x, y = int(point[0]), int(point[1])
        before = metric_fn(img_before, x, y)
        await self._tap(x, y)
        await asyncio.sleep(0.9)
        perc2, img2 = await self._perc_img()
        fd = _frame_diff(img_before, img2)
        result = {"button": name, "point": [x, y], "before_metric": round(before, 1), "frame_diff": round(fd, 3)}

        if fd > 0.06:  # screen changed a lot -> we tapped a nav control
            result["verdict"] = "navigated"
            result["went_to"] = _guess_screen(perc2)
            result["recovered_at"] = await self._recover()
            return result

        after = metric_fn(img2, x, y)
        result["after_metric"] = round(after, 1)
        toggled = abs(after - before) > on_thresh
        result["verdict"] = "hit" if toggled else "missed"
        if toggled:  # restore original state
            await self._tap(x, y)
            await asyncio.sleep(0.7)
            _, img3 = await self._perc_img()
            back = metric_fn(img3, x, y) if img3 else after
            result["restored"] = abs(back - before) < max(20.0, on_thresh)
        return result

    async def test_like(self, point, img_before):
        return await self._toggle_test("like", point, _redness, img_before, on_thresh=45.0)

    async def test_save(self, point, img_before):
        return await self._toggle_test("save", point, _yellowness, img_before, on_thresh=40.0)

    async def _sheet_test(self, name: str, point, img_before, hints):
        """Shared logic for comment/share: tap, verify the sheet opened, dismiss."""
        x, y = int(point[0]), int(point[1])
        await self._tap(x, y)
        await asyncio.sleep(1.1)
        perc2, img2 = await self._perc_img()
        fd = _frame_diff(img_before, img2)
        opened = fd > 0.12 and _has_any(perc2, hints)
        result = {
            "button": name, "point": [x, y], "frame_diff": round(fd, 3),
            "sheet_text_found": _has_any(perc2, hints),
            "verdict": "hit" if opened else ("navigated" if fd > 0.12 else "missed"),
        }
        if not opened and fd > 0.12:
            result["went_to"] = _guess_screen(perc2)
        # Always dismiss whatever opened so the next round starts clean.
        try:
            await self.c.api.swipe(self.did, "down", 0.6)
        except IMouseError:
            pass
        await asyncio.sleep(0.7)
        perc3, _ = await self._perc_img()
        if _guess_screen(perc3) != "unknown":
            await self._recover()
        return result

    async def test_comment(self, point, img_before):
        return await self._sheet_test("comment", point, img_before, COMMENT_HINTS)

    async def test_share(self, point, img_before):
        return await self._sheet_test("share", point, img_before, SHARE_HINTS)


BUTTON_TO_LABEL = {
    "like": "like_button",
    "save": "save_button",
    "comment": "comment_button",
    "share": "share_button",
}


async def run(rounds: int, buttons: list[str]) -> dict:
    cfg = Config()
    async with IMouseClient(cfg) as client:
        registry = DeviceRegistry(client)
        await registry.refresh()
        online = registry.online_devices
        if not online:
            raise SystemExit("No online device. Connect the phone and enable mirroring.")
        controller = TikTokController(cfg, client, registry)
        agent = TikTokAgent(controller)
        dev = online[0]
        st = SelfTest(controller, agent, dev)
        if Image is None:
            print("WARNING: Pillow not installed -- colour verification disabled. "
                  "Run:  pip install pillow")

        report = {"device": dev.device_id, "imgw": st.w, "imgh": st.h, "rounds": []}
        for i in range(rounds):
            perc, img = await st._perc_img()
            scen = _scenario(perc, st.w, st.h)
            pts = agent._ocr_rail_points(perc, st.w, st.h)
            entry = {
                "round": i,
                "scenario": scen,
                "computed_points": {k: list(v) for k, v in pts.items()},
                "right_edge": _right_edge(perc, st.w),
                "tests": [],
            }

            # Only test on an actual feed post (rail counts present).
            if not pts.get("like_button") or img is None:
                entry["skipped"] = "no rail counts (not a feed video?)"
                report["rounds"].append(entry)
                await controller.swipe_next(st.did)
                await asyncio.sleep(1.4)
                continue

            for b in buttons:
                label = BUTTON_TO_LABEL.get(b)
                point = pts.get(label)
                if not point:
                    entry["tests"].append({"button": b, "verdict": "no_point"})
                    continue
                # Re-snapshot right before each test so before-image is current.
                perc_b, img_b = await st._perc_img()
                fn = getattr(st, f"test_{b}")
                try:
                    res = await fn(point, img_b)
                except Exception as exc:  # noqa: BLE001
                    res = {"button": b, "verdict": "error", "error": str(exc)}
                entry["tests"].append(res)
                await asyncio.sleep(0.6)

            report["rounds"].append(entry)
            await controller.swipe_next(st.did)
            await asyncio.sleep(1.4)

        _summarize(report)
        with open("selftest_report.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print("\nFull report written to backend/selftest_report.json")
        return report


def _summarize(report: dict) -> None:
    from collections import defaultdict
    agg = defaultdict(lambda: defaultdict(int))   # (button, scenario) -> verdict -> n
    failures = []
    for rnd in report["rounds"]:
        scen = rnd["scenario"]["label"]
        for t in rnd.get("tests", []):
            v = t.get("verdict", "?")
            agg[(t["button"], scen)][v] += 1
            if v not in ("hit",):
                failures.append((rnd["round"], scen, t, rnd))

    print("\n================ SELF-TEST SUMMARY ================")
    print(f"device {report['device']}  space {report['imgw']}x{report['imgh']}  "
          f"rounds {len(report['rounds'])}")
    print(f"{'button':8} {'scenario':13} {'hit':>4} {'miss':>5} {'nav':>4} {'other':>6}")
    for (button, scen), v in sorted(agg.items()):
        other = sum(n for k, n in v.items() if k not in ("hit", "missed", "navigated"))
        print(f"{button:8} {scen:13} {v['hit']:>4} {v['missed']:>5} {v['navigated']:>4} {other:>6}")

    if failures:
        print(f"\n---- {len(failures)} FAILURE(S) (with rail data for diagnosis) ----")
        for rnd_i, scen, t, rnd in failures[:20]:
            print(f"\nround {rnd_i} [{scen}] {t['button']} -> {t['verdict']}"
                  + (f" (went_to={t.get('went_to')})" if t.get("went_to") else ""))
            print(f"  computed: {rnd['computed_points'].get(BUTTON_TO_LABEL.get(t['button']))}")
            print(f"  right_edge OCR: {[ (e['text'], e['centre']) for e in rnd['right_edge'] ]}")
    else:
        print("\nAll tested taps HIT. \U0001f389")
    print("===================================================")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    btns = (sys.argv[2].split(",") if len(sys.argv) > 2
            else ["like", "save", "comment", "share"])
    asyncio.run(run(n, btns))
