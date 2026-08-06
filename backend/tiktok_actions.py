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

    async def like_video(self, device_id: str, use_double_tap: bool = True) -> ActionResult:
        # Prefer the exact heart icon from the UI map (reliable single tap).
        pt = self._mapped("like_button")
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

    async def save_video(self, device_id: str) -> ActionResult:
        """Save/bookmark the current post."""
        # On this phone save is a direct rail button -- tap it from the map.
        pt = self._mapped("save_button")
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

    async def repost_video(self, device_id: str) -> ActionResult:
        """Open the share sheet (mapped) and tap the Repost entry (by text)."""
        pt = self._mapped("share_button")
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
        try:
            await self.api.swipe(device_id, "down", 0.6)
        except IMouseError:
            pass
        await asyncio.sleep(self.config.action_delay)

    async def open_comments(self, device_id: str) -> ActionResult:
        pt = self._mapped("comment_button")
        if pt:
            return await self._click(device_id, *pt)
        device = await self.get_device(device_id)
        if not device:
            return ActionResult.fail("Device not found")
        x, y = await self._coords(device, "comment_button")
        return await self._click(device_id, x, y)

    async def enter_text(self, device_id: str, text: str) -> ActionResult:
        """Type ``text`` into the focused field.

        ASCII goes through ``/key/sendkey``; anything else uses the clipboard
        paste flow (set clipboard -> brief pause -> WIN+v) so emoji and accented
        characters survive.
        """
        ascii_only = all(ord(c) < 128 for c in text)
        try:
            if ascii_only:
                await self.api.send_key(device_id, key=text)
            else:
                await self.api.clipboard_set(device_id, text)
                await asyncio.sleep(self.config.paste_delay)
                await self.api.send_key(device_id, fn_key="WIN+v")
            return ActionResult.ok("Text entered")
        except IMouseError as exc:
            # The clipboard/paste bridge failed (e.g. transport error). Fall back
            # to the ASCII subset via /key/sendkey so the comment still posts
            # (emoji and accented characters are dropped).
            fallback = "".join(c for c in text if ord(c) < 128).strip()
            if not ascii_only and fallback:
                try:
                    await self.api.send_key(device_id, key=fallback)
                    return ActionResult.ok("Text entered (non-ASCII dropped)")
                except IMouseError:
                    pass
            return ActionResult.fail(str(exc))

    async def comment_on_video(
        self,
        device_id: str,
        comment: Optional[str] = None,
        use_ai: bool = True,
        ai_style: str = "friendly",
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

        # Open the comment sheet and focus the input field.
        if not (await self.open_comments(device_id)).success:
            return ActionResult.fail("Could not open comments")
        await asyncio.sleep(self.config.action_delay)
        ci = self._mapped("comment_input", "comments_sheet")
        if ci:
            await self._click(device_id, *ci)
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
