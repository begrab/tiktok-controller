"""High-level TikTok actions built on the iMouse XP API.

All coordinates go through :meth:`Device.to_pixels`, which maps fractions into
the ``imgw x imgh`` mirror-stream space -- the only space that produces correct
taps. Text targets (Follow, Post) are located with ``/pic/find-text`` rather
than hardcoded coordinates; icon targets fall back to calibratable anchors.

Comment text is entered via the clipboard-then-paste flow so emoji and accented
characters survive (``/key/sendkey`` is ASCII only).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ai_comments import AICommentGenerator, VideoContext, encode_jpeg
from api_client import IMouseClient, IMouseError
from config import Config
from device_manager import Device, DeviceRegistry
from ui_map import get_point

logger = logging.getLogger("tiktok")


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, message: str = "", **data) -> "ActionResult":
        return cls(True, message, data)

    @classmethod
    def fail(cls, message: str) -> "ActionResult":
        return cls(False, message)


class TikTokController:
    """Orchestrates TikTok interactions across one or more devices."""

    def __init__(self, config: Config, client: IMouseClient, registry: DeviceRegistry):
        self.config = config
        self.api = client
        self.registry = registry
        self.ai = AICommentGenerator(config)

    # ------------------------------------------------------------- devices ----
    async def get_device(self, device_id: str) -> Optional[Device]:
        return await self.registry.get_or_refresh(device_id)

    async def _coords(self, device: Device, ui_key: str) -> tuple[int, int]:
        ui = self.config.tiktok_ui.get(ui_key, {"x": 0.5, "y": 0.5})
        return device.to_pixels(ui["x"], ui["y"])

    def _mapped(self, label: str, screen: str = "for_you_feed") -> Optional[tuple[int, int]]:
        """Exact tap point from the user-built UI map, or None if not mapped."""
        pt = get_point(screen, label)
        return (int(pt[0]), int(pt[1])) if pt else None

    # ------------------------------------------------------- mirror recovery --
    async def screenshot_works(self, device_id: str) -> bool:
        """True if the AirPlay mirror is up (a screenshot succeeds)."""
        try:
            await self.api.screenshot(device_id, binary=True)
            return True
        except IMouseError:
            return False

    async def recover_mirror(self, device_id: str) -> bool:
        """Try to re-establish the dropped AirPlay mirror from the phone side.

        Control still works over USB even when the mirror is down, so we drive
        the phone's Control Center (opened with the ``ControlBar`` hotkey) and
        re-tap Screen Mirroring -> the PC receiver, using coordinates calibrated
        once while the mirror was up (``control_center`` in the UI map). Returns
        True only if a screenshot succeeds afterwards. Returns False (so the
        caller stops cleanly) when it isn't calibrated or the re-cast didn't take.
        """
        mirror_btn = get_point("control_center", "screen_mirroring")
        receiver = get_point("control_center", "airplay_receiver")
        if not mirror_btn:
            return False  # not calibrated -- can't safely blind-tap Control Center
        try:
            await self.api.send_key(device_id, fn_key="ControlBar")
            await asyncio.sleep(1.3)
            await self.api.click(device_id, int(mirror_btn[0]), int(mirror_btn[1]))
            await asyncio.sleep(1.6)
            if receiver:
                await self.api.click(device_id, int(receiver[0]), int(receiver[1]))
                await asyncio.sleep(3.5)  # AirPlay needs a moment to re-establish
            # Close Control Center with a swipe-down -- NOT the Home button, which
            # would drop the phone to the iOS home screen and exit TikTok.
            await self.api.swipe(device_id, "down", 0.5)
            await asyncio.sleep(1.5)
        except IMouseError:
            return False
        return await self.screenshot_works(device_id)

    # ---------------------------------------------------------- screenshots ---
    async def get_screenshot_bytes(self, device_id: str) -> bytes:
        """Raw JPEG bytes of the current screen."""
        return await self.api.screenshot(device_id, binary=True)

    async def get_screenshot_b64(self, device_id: str) -> str:
        """Base64 JPEG, for the web UI and the vision API."""
        return encode_jpeg(await self.get_screenshot_bytes(device_id))

    async def analyze_current_video(self, device_id: str) -> VideoContext:
        """Read the current video via vision + OCR into a :class:`VideoContext`."""
        context = VideoContext()

        try:
            context.screenshot_base64 = await self.get_screenshot_b64(device_id)
        except IMouseError as exc:
            logger.warning("screenshot for analysis failed: %s", exc)
            return context

        # Vision analysis (only if an Anthropic key is configured).
        if context.screenshot_base64 and self.config.anthropic_api_key:
            try:
                analysis = await self.ai.analyze_screenshot(context.screenshot_base64)
                context.caption = analysis.get("caption") or None
                context.hashtags = analysis.get("hashtags") or []
                context.username = analysis.get("username") or None
                context.ocr_text = analysis.get("content_description") or None
            except Exception as exc:  # noqa: BLE001 - never let AI break analysis
                logger.warning("vision analysis failed: %s", exc)

        # OCR the caption region as a supplement. Gracefully skip if the OCR
        # plugin is not running (code 28).
        device = await self.get_device(device_id)
        if device and device.has_coord_space:
            area = self.config.tiktok_ui.get("caption_area")
            rect = None
            if area:
                x0, y0 = device.to_pixels(area["x"], area["y"])
                x1, y1 = device.to_pixels(area["x"] + area["w"], area["y"] + area["h"])
                rect = [x0, y0, x1, y1]
            try:
                result = await self.api.ocr(device_id, rect=rect)
                texts = [i.get("text", "") for i in result.items if i.get("text")]
                if texts:
                    ocr_text = " ".join(texts)
                    context.ocr_text = ocr_text
                    context.hashtags = sorted(
                        set(context.hashtags) | set(re.findall(r"#\w+", ocr_text))
                    )
            except IMouseError as exc:
                if exc.code == 28:
                    logger.info("OCR skipped: plugin not connected (code 28)")
                else:
                    logger.warning("OCR failed: %s", exc)

        return context

    # ---------------------------------------------------------- core taps -----
    async def _click(self, device_id: str, x: int, y: int) -> ActionResult:
        # Let the relative-mouse pointer come to rest before tapping, so the
        # click doesn't fire while the pointer is still travelling to (x, y).
        if self.config.pre_tap_settle:
            await asyncio.sleep(self.config.pre_tap_settle)
        try:
            await self.api.click(device_id, x, y)
            return ActionResult.ok()
        except IMouseError as exc:
            return ActionResult.fail(str(exc))

    async def swipe_next(self, device_id: str) -> ActionResult:
        try:
            await self.api.swipe(device_id, "up", self.config.swipe_length)
            # A swipe drags the pointer; give it a beat to settle before any tap.
            await asyncio.sleep(self.config.post_swipe_settle)
            return ActionResult.ok("Swiped to next video")
        except IMouseError as exc:
            return ActionResult.fail(str(exc))

    async def swipe_previous(self, device_id: str) -> ActionResult:
        try:
            await self.api.swipe(device_id, "down", self.config.swipe_length)
            await asyncio.sleep(self.config.post_swipe_settle)
            return ActionResult.ok("Swiped to previous video")
        except IMouseError as exc:
            return ActionResult.fail(str(exc))

    async def like_video(
        self,
        device_id: str,
        use_double_tap: bool = True,
        point: Optional[tuple[int, int]] = None,
    ) -> ActionResult:
        # Priority: a per-frame vision-located point (adapts to the rail shifting
        # up/down), then the static UI map, then the double-tap/anchor fallback.
        pt = point or self._mapped("like_button")
        if pt:
            result = await self._click(device_id, *pt)
            result.message = "Liked" if result.success else result.message
            return result

        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        if use_double_tap:  # fallback: double-tap the video centre
            x, y = await self._coords(device, "video_center")
            try:
                await self.api.click(device_id, x, y)
                await asyncio.sleep(0.12)
                await self.api.click(device_id, x, y)
                return ActionResult.ok("Liked (double-tap)")
            except IMouseError as exc:
                return ActionResult.fail(str(exc))
        x, y = await self._coords(device, "like_button")
        result = await self._click(device_id, x, y)
        result.message = "Liked" if result.success else result.message
        return result

    async def save_video(
        self, device_id: str, point: Optional[tuple[int, int]] = None
    ) -> ActionResult:
        """Save/bookmark the current post."""
        # Priority: per-frame vision point, then the static map, then share-sheet.
        pt = point or self._mapped("save_button")
        if pt:
            result = await self._click(device_id, *pt)
            result.message = "Saved video" if result.success else result.message
            return result

        # Fallback: open the share sheet and find the Save entry by text.
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        x, y = await self._coords(device, "share_button")
        if not (await self._click(device_id, x, y)).success:
            return ActionResult.fail("Could not open share menu")
        await asyncio.sleep(self.config.action_delay)
        if await self._tap_text(device_id, ["Save", "Favorites", "Add to Favorites"]):
            return ActionResult.ok("Saved video")
        await self._dismiss(device_id)
        return ActionResult.fail("Save option not found (share menu text not matched)")

    async def repost_video(
        self, device_id: str, point: Optional[tuple[int, int]] = None
    ) -> ActionResult:
        """Open the share sheet and tap the Repost entry (by text)."""
        pt = point or self._mapped("share_button")
        if pt:
            opened = await self._click(device_id, *pt)
        else:
            device = await self.get_device(device_id)
            if not device:
                return ActionResult.fail("Device not found")
            x, y = await self._coords(device, "share_button")
            opened = await self._click(device_id, x, y)
        if not opened.success:
            return ActionResult.fail("Could not open share menu")
        await asyncio.sleep(self.config.action_delay)

        if await self._tap_text(device_id, ["Repost", "Re-post"]):
            return ActionResult.ok("Reposted video")
        await self._dismiss(device_id)
        return ActionResult.fail("Repost option not found (share menu text not matched)")

    async def return_to_feed(self, device_id: str, attempt: int = 0) -> ActionResult:
        """Coordinate-free fallback recovery: hardware Back (+ swipe-down).

        Blind coordinate taps here proved dangerous (top-left = LIVE, wrong tab =
        DMs), so the agent uses the vision-guided recovery in ai_agent instead.
        This method stays as a safe, tap-free fallback for any other caller.
        """
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        try:
            await self.api.send_key(device_id, fn_key="TAB+b")
        except IMouseError:
            pass
        await asyncio.sleep(self.config.action_delay)
        if attempt >= 2:
            try:
                await self.api.swipe(device_id, "down", 0.5)
            except IMouseError:
                pass
            await asyncio.sleep(self.config.action_delay)
        return ActionResult.ok("Return-to-feed attempted (back)")

    async def follow_creator(self, device_id: str) -> ActionResult:
        if await self._tap_text(device_id, ["Follow"]):
            return ActionResult.ok("Followed creator")
        return ActionResult.fail("Follow button not found")

    # ------------------------------------------------------------ comments ----
    async def _tap_text(
        self, device_id: str, candidates: list[str], similarity: Optional[float] = None
    ) -> bool:
        """Find any of ``candidates`` on screen and tap its centre. False if none."""
        try:
            matches = await self.api.find_text(
                device_id,
                candidates,
                similarity=similarity or self.config.ocr_similarity,
            )
        except IMouseError as exc:
            if exc.code == 28:
                logger.info("find-text needs the OCR plugin (code 28)")
            else:
                logger.warning("find-text failed: %s", exc)
            return False

        for match in matches:
            centre = match.get("centre") or match.get("center")
            if centre and len(centre) == 2:
                await self.api.click(device_id, int(centre[0]), int(centre[1]))
                return True
        return False

    async def _dismiss(self, device_id: str) -> None:
        """Best-effort: close an open comment/share sheet or keyboard.

        A downward swipe reliably dismisses TikTok's comment/share sheets without
        the risk of a blind tap landing on a navigation control (LIVE, a tab,
        etc.). Used on every comment/save/repost exit path so the agent never
        gets stuck in a dialog with no way back.
        """
        # A downward swipe closes the comment sheet, but TikTok's SHARE sheet
        # often ignores it -- the reliable close is tapping the dimmed backdrop
        # above the sheet. Do both so either sheet ends up dismissed.
        try:
            await self.api.swipe(device_id, "down", 0.85)
        except IMouseError:
            pass
        await asyncio.sleep(self.config.action_delay)
        device = await self.get_device(device_id)
        if device:
            # Tap the dimmed backdrop above the sheet -- but well BELOW the top
            # tab bar (Gefolgt / Für dich / search at ~y=0.06), which would
            # switch feeds or open search. y=0.22 is pure scrim.
            bx, by = device.to_pixels(0.3, 0.22)
            try:
                await self._click(device_id, bx, by)
            except IMouseError:
                pass
        await asyncio.sleep(self.config.action_delay)

    async def _find_comment_input(self, device_id: str) -> Optional[tuple[int, int]]:
        """Locate the 'Add comment' input BAR (bottom of the sheet) so we type a
        top-level comment -- not a reply on an existing comment. Returns the tap
        point, or None if the placeholder text isn't found."""
        try:
            # ASCII-safe fragments (OCR mangles umlauts and truncates with '…').
            hits = await self.api.find_text(
                device_id,
                ["hinzu", "Kommentar hinzu", "Add comment", "Sag etwas", "Nettes"],
                similarity=0.5,
            )
        except IMouseError:
            return None
        device = await self.get_device(device_id)
        h = (device.imgh or device.height) if device else 1080
        best = None
        for hit in hits:
            c = hit.get("centre") or hit.get("center")
            # The input bar is at the very BOTTOM of the sheet; ignore the
            # "Kommentare N" header and the comment list above it.
            if c and len(c) == 2 and c[1] > h * 0.82:
                if best is None or c[1] > best[1]:
                    best = (int(c[0]), int(c[1]))
        return best

    async def _comment_sheet_open(self, device_id: str) -> bool:
        """True if the comment sheet is showing (its header/CTA text is visible)."""
        try:
            hits = await self.api.find_text(
                device_id,
                ["Kommentar", "Kommentare", "Comments", "Comment", "Kommentieren"],
                similarity=0.7,
            )
            return bool(hits)
        except IMouseError:
            return False  # OCR unavailable -- caller falls back to best-effort

    async def open_comments(
        self, device_id: str, point: Optional[tuple[int, int]] = None
    ) -> ActionResult:
        pt = point or self._mapped("comment_button")
        if pt:
            return await self._click(device_id, *pt)
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        x, y = await self._coords(device, "comment_button")
        return await self._click(device_id, x, y)

    # Transliteration for the typed fallback so German still reads correctly if
    # the clipboard bridge is unavailable (ä->ae etc.).
    _TRANSLIT = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        "á": "a", "à": "a", "â": "a", "é": "e", "è": "e", "ê": "e", "í": "i", "ì": "i",
        "ó": "o", "ò": "o", "ô": "o", "ú": "u", "ù": "u", "ñ": "n", "ç": "c",
        "–": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
    }

    @classmethod
    def _to_ascii(cls, text: str) -> str:
        out = "".join(cls._TRANSLIT.get(c, c) for c in text)
        return "".join(c for c in out if ord(c) < 128)

    _ZY_SWAP = str.maketrans({"z": "y", "y": "z", "Z": "Y", "Y": "Z"})

    def _for_typing(self, text: str) -> str:
        """Prepare text for /key/sendkey: transliterate to ASCII, then undo the
        QWERTZ z<->y swap so a German phone renders it correctly."""
        ascii_text = self._to_ascii(text)
        if self.config.keyboard_swap_zy:
            ascii_text = ascii_text.translate(self._ZY_SWAP)
        return ascii_text

    async def enter_text(self, device_id: str, text: str) -> ActionResult:
        """Enter ``text`` into the focused field.

        ALWAYS prefer the clipboard paste flow (set clipboard -> pause -> WIN+v):
        it is keyboard-LAYOUT-INDEPENDENT and preserves umlauts/emoji, whereas
        /key/sendkey types via scancodes -- on a German (QWERTZ) phone that swaps
        z<->y and mangles non-ASCII. Only if the clipboard bridge is unavailable
        do we type a transliterated ASCII version so the comment still posts.
        """
        exc: Optional[Exception] = None
        if self.config.use_clipboard_paste:
            try:
                # Short timeout so a missing clipboard bridge fails fast instead
                # of hanging ~30s per comment.
                await asyncio.wait_for(self.api.clipboard_set(device_id, text), timeout=6.0)
                await asyncio.sleep(self.config.paste_delay)
                await self.api.send_key(device_id, fn_key="WIN+v")
                return ActionResult.ok("Text entered (paste)")
            except (IMouseError, asyncio.TimeoutError) as e:
                exc = e  # bridge unavailable -- type instead

        fallback = self._for_typing(text).strip()
        if fallback:
            try:
                await self.api.send_key(device_id, key=fallback)
                return ActionResult.ok("Text entered (typed)")
            except IMouseError as e:
                exc = e
        return ActionResult.fail(f"could not enter text: {exc}")

    async def comment_on_video(
        self,
        device_id: str,
        comment: Optional[str] = None,
        use_ai: bool = True,
        ai_style: str = "friendly",
        comment_button_point: Optional[tuple[int, int]] = None,
    ) -> ActionResult:
        """Full flow: open comments, (optionally) generate, type, and post."""
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")

        if comment is None and use_ai:
            context = await self.analyze_current_video(device_id)
            try:
                comment = await self.ai.generate_comment(context, style=ai_style)
            except Exception as exc:  # noqa: BLE001
                return ActionResult.fail(f"AI comment generation failed: {exc}")
        if not comment:
            return ActionResult.fail("No comment text available")

        # Open the comment sheet. We only re-tap the comment ICON if the sheet is
        # still closed (tapping it again while OPEN would hit a comment in the
        # sheet and start a REPLY). Prefer the real "Add comment" input bar,
        # located by its placeholder text, so we post a TOP-LEVEL comment.
        opened = False
        input_pt: Optional[tuple[int, int]] = None
        for _ in range(2):
            input_pt = await self._find_comment_input(device_id)
            if input_pt or await self._comment_sheet_open(device_id):
                opened = True
                break
            await self.open_comments(device_id, point=comment_button_point)
            await asyncio.sleep(self.config.action_delay)
        if not opened:
            input_pt = await self._find_comment_input(device_id)
            opened = bool(input_pt) or await self._comment_sheet_open(device_id)
        if not opened:
            return ActionResult.fail("Could not open comments")

        # Tap the located input bar; fall back to the mapped/anchor point.
        if not input_pt:
            input_pt = self._mapped("comment_input", "comments_sheet")
        if input_pt:
            await self._click(device_id, *input_pt)
        else:
            x, y = await self._coords(device, "comment_input")
            await self._click(device_id, x, y)
        await asyncio.sleep(self.config.paste_delay)

        entered = await self.enter_text(device_id, comment)
        if not entered.success:
            # Never leave the keyboard/comment sheet open -- return to the feed.
            await self._dismiss(device_id)
            return ActionResult.fail(f"Could not enter comment: {entered.message}")
        await asyncio.sleep(self.config.paste_delay)

        # Post: prefer the labelled Send/Post button, else Enter.
        posted = await self._tap_text(device_id, ["Post", "Send"])
        if not posted:
            try:
                await self.api.send_key(device_id, fn_key="ENTER")
                posted = True
            except IMouseError:
                posted = False

        # Always dismiss the comment sheet so the next step starts clean.
        await asyncio.sleep(self.config.action_delay)
        await self._dismiss(device_id)

        if posted:
            return ActionResult.ok("Comment posted", comment=comment)
        return ActionResult.fail("Could not post comment")

    async def generate_comment_options(self, device_id: str, count: int = 3) -> list[str]:
        context = await self.analyze_current_video(device_id)
        return await self.ai.generate_multiple_comments(context, count=count)

    # ------------------------------------------------------------- batch ------
    async def batch_action(self, device_ids: list[str], action: str, **kwargs) -> dict:
        actions = {
            "swipe_next": self.swipe_next,
            "swipe_previous": self.swipe_previous,
            "like": self.like_video,
            "save": self.save_video,
            "follow": self.follow_creator,
        }
        fn = actions.get(action)
        if fn is None:
            return {d: ActionResult.fail(f"Unknown action: {action}") for d in device_ids}

        results = await asyncio.gather(
            *(fn(d, **kwargs) for d in device_ids), return_exceptions=True
        )
        out = {}
        for device_id, res in zip(device_ids, results):
            out[device_id] = (
                ActionResult.fail(str(res)) if isinstance(res, Exception) else res
            )
        return out

    # --------------------------------------------------------- automation -----
    async def auto_browse(
        self,
        device_id: str,
        video_count: int = 10,
        like_probability: float = 0.3,
        comment_probability: float = 0.1,
        min_watch_time: float = 3.0,
        max_watch_time: float = 15.0,
    ) -> dict:
        """Browse with randomised human-like engagement."""
        import random

        stats = {"videos": 0, "likes": 0, "comments": 0, "errors": []}
        for _ in range(video_count):
            try:
                await asyncio.sleep(random.uniform(min_watch_time, max_watch_time))
                if random.random() < like_probability:
                    if (await self.like_video(device_id)).success:
                        stats["likes"] += 1
                if random.random() < comment_probability:
                    if (await self.comment_on_video(device_id, use_ai=True)).success:
                        stats["comments"] += 1
                await self.swipe_next(device_id)
                stats["videos"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(str(exc))
        return stats
