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
# Screen identity is decided by OCR, NOT by a pixel diff -- the feed video is
# always in motion, so a frame diff can't tell a like from a navigation.
#
# The decisive feed signal is the right-hand RAIL of count numbers. Sheets cover
# it; profiles/search replace it. NOTE: 'Gefolgt'/'Für dich' are the feed's own
# top tabs and 'Profile' is the bottom nav label -- they are NOT off-feed signals
# and must never be treated as such.
COMMENT_HINTS = ("kommentar", "comment", "antworten", "reply", "kommentieren")
SHARE_HINTS = ("whatsapp", "link kopieren", "copy link", "senden an", "an freunde senden",
               "repost", "teilen mit", "share to", "story hinzu", "zu story", "in nachricht",
               "duett", "stitch", "als nachricht", "einbetten", "melden", "kein interesse",
               "instagram", "messenger", "telegram")
PROFILE_HINTS = ("follower", "abonnent", "abonniert", "beiträge", "gefällt mir", "reposts")


def _texts(perc: dict) -> list[str]:
    return [(e.get("text") or "").lower() for e in perc.get("elements", [])]


def _has_any(perc: dict, hints) -> bool:
    joined = " ".join(_texts(perc))
    return any(h in joined for h in hints)


def _is_count_text(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.match(r"^[\d][\d.,]*\s*(?:[KkMm]|万|Mio\.?|Tsd\.?)?$", t))


def _rail_counts(perc: dict, w: int, h: int) -> list:
    out = []
    for e in perc.get("elements", []):
        cx, cy = e["centre"]
        if cx > w * 0.85 and h * 0.12 < cy < h * 0.90 and _is_count_text(e["text"]):
            out.append((int(cy), int(cx), e["text"]))
    out.sort()
    return out


# Rail slots may show a WORD label instead of a count on low-engagement posts.
RAIL_WORDS = ("teilen", "speicher", "favorit", "merken", "gespeichert", "share", "save")


def _rail_markers(perc: dict, w: int, h: int) -> list:
    """Right-edge rail entries -- counts OR label words (Teilen/Speichern)."""
    out = []
    for e in perc.get("elements", []):
        cx, cy = e["centre"]
        if not (cx > w * 0.85 and h * 0.12 < cy < h * 0.90):
            continue
        t = (e.get("text") or "").strip().lower()
        if _is_count_text(t) or any(k in t for k in RAIL_WORDS):
            out.append((int(cy), int(cx)))
    return sorted(out)


def is_feed(perc: dict, w: int, h: int) -> bool:
    """A feed video shows >=2 rail entries (counts or label words) on the right."""
    return len(_rail_markers(perc, w, h)) >= 2


def classify_screen(perc: dict, w: int, h: int) -> str:
    """feed | share_sheet | comments | profile | unknown (OCR-based)."""
    if is_feed(perc, w, h):
        return "feed"
    if _has_any(perc, SHARE_HINTS):
        return "share_sheet"
    if _has_any(perc, COMMENT_HINTS):
        return "comments"
    if _has_any(perc, PROFILE_HINTS):
        return "profile"
    return "unknown"


def _scenario(perc: dict, w: int, h: int) -> dict:
    texts = [e.get("text", "") for e in perc.get("elements", [])]
    slideshow = any(re.search(r"\b\d+\s*/\s*\d+\b", t) for t in texts)
    rail = _rail_counts(perc, w, h)
    top = rail[0][0] if rail else None
    # 'shifted' when the rail rides high (bottom overlay pushed it up) -- the case
    # that used to break fixed coordinates.
    if slideshow:
        label = "slideshow"
    elif top is not None and top < h * 0.46:
        label = "rail_shifted"
    else:
        label = "video"
    return {"label": label, "rail_top_y": top, "n_rail_counts": len(rail)}


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

    async def _screen(self) -> tuple[str, dict]:
        perc, _ = await self._perc_img()
        return classify_screen(perc, self.w, self.h), perc

    async def _return_to_feed(self, max_tries: int = 5) -> str:
        """Get back to a feed video, whatever we're stuck on. Verified by OCR.

        Bottom sheets (share/comment) do NOT reliably close on a swipe-down, so
        we tap the dimmed backdrop above the sheet; profiles/search use hardware
        Back. We re-check the screen (via the rail) after every attempt.
        """
        for i in range(max_tries):
            screen, _ = await self._screen()
            if screen == "feed":
                return "feed"
            # The share sheet often classifies as 'unknown' (its text doesn't
            # match), so DON'T gate the backdrop tap on the classification: a tap
            # on the dimmed strip above any bottom sheet closes it and is
            # harmless on a feed video. Try that first on every off-feed screen.
            await self._tap(int(self.w * 0.3), int(self.h * 0.22))
            await asyncio.sleep(0.6)
            if (await self._screen())[0] == "feed":
                return "feed"
            # Then a downward swipe (closes the comment sheet), then hardware Back
            # (for profile/search). Escalate across attempts.
            try:
                if i % 2 == 0:
                    await self.c.api.swipe(self.did, "down", 0.9)
                else:
                    await self.c.api.send_key(self.did, fn_key="TAB+b")
            except IMouseError:
                pass
            await asyncio.sleep(0.8)
        return (await self._screen())[0]

    async def _toggle_test(self, name: str, point, metric_fn, img_before, on_thresh: float):
        """like/save: tap, then decide by OCR (navigation) + icon colour (toggle)."""
        x, y = int(point[0]), int(point[1])
        before = metric_fn(img_before, x, y)
        await self._tap(x, y)
        await asyncio.sleep(0.9)
        perc2, img2 = await self._perc_img()
        screen = classify_screen(perc2, self.w, self.h)
        result = {"button": name, "point": [x, y], "before_metric": round(before, 1)}

        if screen != "feed":
            # Re-check once: a like doesn't leave the feed, so a transient 'not
            # feed' is usually a mid-frame OCR miss. Only a confirmed off-feed is
            # a real navigation.
            await asyncio.sleep(0.5)
            perc2, img2 = await self._perc_img()
            screen = classify_screen(perc2, self.w, self.h)
        if screen != "feed":  # confirmed: we left the video -> tapped a nav control
            result["verdict"] = "navigated"
            result["went_to"] = screen
            result["recovered_at"] = await self._return_to_feed()
            return result

        after = metric_fn(img2, x, y) if img2 else before
        result["after_metric"] = round(after, 1)
        toggled = abs(after - before) > on_thresh
        result["verdict"] = "hit" if toggled else "missed"
        if toggled:  # restore original state so the account is left untouched
            await self._tap(x, y)
            await asyncio.sleep(0.7)
            _, img3 = await self._perc_img()
            back = metric_fn(img3, x, y) if img3 else after
            result["restored"] = abs(back - before) < max(25.0, on_thresh)
        return result

    async def test_like(self, point, img_before):
        return await self._toggle_test("like", point, _redness, img_before, on_thresh=45.0)

    async def test_save(self, point, img_before):
        return await self._toggle_test("save", point, _yellowness, img_before, on_thresh=40.0)

    async def _sheet_test(self, name: str, point, img_before, expect: str):
        """comment/share: tap, verify the RIGHT sheet opened (OCR), then dismiss."""
        x, y = int(point[0]), int(point[1])
        await self._tap(x, y)
        await asyncio.sleep(1.1)
        screen, _ = await self._screen()
        if screen == expect:
            verdict = "hit"
        elif screen == "feed":
            verdict = "missed"          # tapped dead space, sheet never opened
        else:
            verdict = "navigated"       # opened the wrong thing
        result = {"button": name, "point": [x, y], "screen_after": screen, "verdict": verdict}
        result["recovered_at"] = await self._return_to_feed()
        return result

    async def test_comment(self, point, img_before):
        return await self._sheet_test("comment", point, img_before, "comments")

    async def test_share(self, point, img_before):
        return await self._sheet_test("share", point, img_before, "share_sheet")


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

            # Only test on an actual feed post (rail present), not a stray
            # transitional/overlay frame.
            if img is None or not is_feed(perc, st.w, st.h) or not pts.get("like_button"):
                entry["skipped"] = "not a feed video (no rail)"
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
            extra = t.get("went_to") or t.get("screen_after")
            print(f"\nround {rnd_i} [{scen}] {t['button']} -> {t['verdict']}"
                  + (f" (screen={extra})" if extra else "")
                  + (f" recovered={t.get('recovered_at')}" if t.get("recovered_at") else ""))
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
