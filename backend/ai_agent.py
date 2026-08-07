"""Claude-driven perceive -> decide -> act agent for TikTok.

Each step:
  1. Perceive: capture a screenshot and run OCR to get on-screen text elements,
     each with a tap ``centre`` already in the ``imgw x imgh`` space.
  2. Decide: hand Claude the screenshot + the OCR element list + the goal and a
     small set of tools; Claude calls exactly one tool (forced tool use).
  3. Act: execute that tool against the device via the controller.

Text targets are grounded in real coordinates by tapping OCR/find-text centres,
so Claude never has to guess pixel positions. Icon targets fall back to the
calibratable anchors. OCR needs the PaddleOCR plugin; if it isn't running
(code 28) the agent degrades to vision-only reasoning (swipe / like / comment
via anchors), which is surfaced in the step log.

Cost control follows the handover: each decision sends only the *current*
screenshot plus a short text history of prior actions -- earlier screenshots are
never re-sent.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from api_client import IMouseError
from niche import NICHE
from tiktok_actions import ActionResult, TikTokController

logger = logging.getLogger("ai_agent")


# Tools Claude may call. One action per step (forced, non-parallel).
TOOLS: list[dict] = [
    {
        "name": "swipe_next",
        "description": "Scroll to the next video (swipe up). Use when the current video isn't interesting or you're done engaging with it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "swipe_previous",
        "description": "Go back to the previous video (swipe down).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "like",
        "description": "Like the current video (double-tap heart). Use when the content is genuinely good.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save",
        "description": "Save/bookmark the current video for later.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "repost",
        "description": "Repost the current video (via the share sheet). Use for something you'd want to share on.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "return_to_feed",
        "description": "Go back to the main For You feed by tapping Home. Use this whenever the screen is NOT a normal playing For You video -- a profile page, comments, share sheet, search, DMs, or any menu -- instead of trying to interact with it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "comment",
        "description": "Post a short, genuine, relevant comment on the current video. This handles opening the comment box, typing, posting, and closing it -- you do not tap anything yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The comment text to post."}
            },
            "required": ["text"],
        },
    },
    {
        "name": "stop",
        "description": "End the session -- nothing useful left to do, or the goal is met.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why you're stopping."}
            },
        },
    },
]

DEFAULT_GOAL = (
    "Browse TikTok like a real, curious human. For each video: read what it's "
    "about, like it if it's genuinely good, occasionally leave a short relevant "
    "comment or save/repost something you really love, then move to the next "
    "video. Don't engage with everything -- be selective and natural."
)

SYSTEM_PROMPT = (
    "You are operating a real iPhone running the TikTok For You feed, one action "
    "at a time. You see the current screen as an image plus a list of on-screen "
    "text detected by OCR. Use both to understand exactly what is on screen "
    "right now, then call exactly one tool.\n\n"
    "You may ONLY perform these actions: swipe_next, swipe_previous, like, save, "
    "repost, comment, return_to_feed, or stop. There is no other action. Never "
    "open the camera or any create/record/post-a-video screen, and never tap "
    "arbitrary buttons.\n\n"
    "Before acting, check what screen you're on. like/save/repost/comment are "
    "ONLY valid on a normal, full-screen For You video that's playing. If you "
    "see a profile page (avatar, follower counts, a grid of videos, tabs like "
    "Videos/Reposts/Liked), an open comment or share sheet, search, DMs, "
    "settings, an ad, or a login/permission dialog, do NOT try to interact with "
    "it -- call return_to_feed to get back to browsing.\n\n"
    "The comment action is self-contained: it opens the box, types, posts, and "
    "closes it for you. Behave like a genuine, selective human: don't like or "
    "comment on everything, vary your actions, and keep comments short, "
    "specific, and authentic."
)


class TikTokAgent:
    """Wraps a controller with a Claude decision loop."""

    def __init__(self, controller: TikTokController):
        self.controller = controller
        self.config = controller.config
        self._campaign = self._fresh_campaign_state()
        self._last_locate_source = "map"  # ocr | vision | map (for the log tag)

    @staticmethod
    def _fresh_campaign_state() -> dict:
        return {
            "running": False,
            "stop": False,
            "device_id": None,
            "duration": 0,
            "elapsed": 0.0,
            "stats": {
                "videos": 0, "relevant": 0, "likes": 0, "saves": 0,
                "comments": 0, "recovered": 0, "errors": 0,
            },
            "log": [],
            "used_comments": [],
            "last_screenshot": None,   # b64 of the most recent frame, for the UI
            "off_feed_streak": 0,      # consecutive off-feed cycles (escalates recovery)
            "error_streak": 0,         # consecutive perceive errors (device offline etc.)
            "videos_since_comment": 999,  # relevant videos since the last comment
            "comment_gap": 0,          # randomized cooldown target (set on first use)
            "engaged_posts": [],       # word-sets of posts already engaged (no re-liking)
            "next_perc": None,         # settled frame from the last advance (reused, saves a capture)
        }

    # --------------------------------------------------------- perceive ------
    async def perceive(self, device_id: str) -> dict:
        """Capture the screen and OCR elements for one decision."""
        result: dict[str, Any] = {"screenshot_b64": None, "elements": [], "ocr_available": False}

        try:
            result["screenshot_b64"] = await self.controller.get_screenshot_b64(device_id)
        except IMouseError as exc:
            result["error"] = f"screenshot failed: {exc}"
            result["error_code"] = exc.code  # 25 = device offline, 28 = OCR plugin
            return result

        try:
            ocr = await self.controller.api.ocr(device_id)
            elements = []
            for item in ocr.items:
                text = item.get("text")
                centre = item.get("centre") or item.get("center")
                if text and centre and len(centre) == 2:
                    elements.append({"text": text, "centre": [int(centre[0]), int(centre[1])]})
            result["elements"] = elements
            result["ocr_available"] = True
        except IMouseError as exc:
            result["ocr_error"] = (
                "OCR plugin not connected (code 28)" if exc.code == 28 else str(exc)
            )

        return result

    async def settled_perception(self, device_id: str, tries: int = 3) -> dict:
        """Perceive a STABLE frame.

        The AirPlay mirror lags the phone by up to a second or two, so a capture
        taken right after a swipe/navigation can show the *previous* video or a
        transitional menu that's already gone -- which made the campaign mark
        OFF-FEED while actually on the feed, or engage the wrong video. We wait
        until two consecutive captures agree (same fingerprint) before trusting
        the frame.
        """
        perc = await self.perceive(device_id)
        for _ in range(max(0, tries - 1)):
            if perc.get("error"):
                return perc
            words1 = self._post_words(perc)
            await asyncio.sleep(0.45)
            nxt = await self.perceive(device_id)
            if nxt.get("error"):
                return nxt
            words2 = self._post_words(nxt)
            # Settled when the two captures describe the same thing (or both empty).
            if (words1 == words2) or self._same_post(words1, words2):
                return nxt
            perc = nxt
        return perc

    # ---------------------------------------------------------- decide -------
    async def decide(
        self, perception: dict, goal: str, history: list[str]
    ) -> dict:
        """Ask Claude for the next action. Returns {"name", "input"}."""
        client = self.controller.ai.client  # AsyncAnthropic; raises if no key

        elements = perception.get("elements", [])
        if elements:
            elements_text = "Text currently on screen (OCR) -- use it to understand what's showing:\n" + "\n".join(
                f"- {e['text']}" for e in elements
            )
        elif perception.get("ocr_available"):
            elements_text = "No text detected on screen (likely a plain video)."
        else:
            elements_text = (
                "OCR is unavailable ("
                + perception.get("ocr_error", "unknown")
                + "); read any text from the image instead."
            )

        history_text = (
            "Recent actions:\n" + "\n".join(f"- {h}" for h in history[-8:])
            if history
            else "No actions yet."
        )

        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": perception["screenshot_b64"],
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": f"Goal: {goal}\n\n{elements_text}\n\n{history_text}\n\n"
                "Decide the single next action and call one tool.",
            }
        )

        resp = await client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_choice={"type": "any", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": content}],
        )

        for block in resp.content:
            if block.type == "tool_use":
                return {"name": block.name, "input": dict(block.input or {})}

        # Forced tool use should always yield a tool_use block; guard anyway.
        return {"name": "stop", "input": {"reason": "no tool call returned"}}

    # ----------------------------------------------------------- act ---------
    async def execute(self, device_id: str, name: str, args: dict) -> ActionResult:
        c = self.controller
        if name == "swipe_next":
            return await c.swipe_next(device_id)
        if name == "swipe_previous":
            return await c.swipe_previous(device_id)
        # Rail actions: locate the icons on THIS frame first so the tap tracks the
        # (shifting) rail instead of a stale fixed pixel.
        if name in ("like", "save", "repost", "comment"):
            pts = await self.locate_actions(device_id)
            label = {"like": "like_button", "save": "save_button",
                     "repost": "share_button", "comment": "comment_button"}[name]
            hit = pts.get(label)
            src = f"{self._last_locate_source}@{hit}" if hit else "map"
            if name == "like":
                res = await c.like_video(device_id, point=hit)
            elif name == "save":
                res = await c.save_video(device_id, point=hit)
            elif name == "repost":
                res = await c.repost_video(device_id, point=hit)
            else:
                text = args.get("text", "")
                if not text:
                    return ActionResult.fail("comment called without text")
                res = await c.comment_on_video(
                    device_id, comment=text, use_ai=False, comment_button_point=hit
                )
            res.message = f"{res.message} [{src}]"
            return res
        if name == "return_to_feed":
            perc = await self.perceive(device_id)
            move = await self.recover(device_id, perc)
            await self.apply_recovery(device_id, move)
            return ActionResult.ok(f"recover: {move.get('method')} — {move.get('note', '')}")
        if name == "stop":
            return ActionResult.ok(f"Stopped: {args.get('reason', '')}")
        return ActionResult.fail(f"Unknown action: {name}")

    # ------------------------------------------------------------ loop -------
    async def step(self, device_id: str, goal: Optional[str] = None, history: Optional[list[str]] = None) -> dict:
        """One perceive -> decide -> act cycle. Returns a log entry."""
        goal = goal or DEFAULT_GOAL
        history = history or []

        perception = await self.perceive(device_id)
        if perception.get("error"):
            return {"action": None, "success": False, "message": perception["error"]}

        decision = await self.decide(perception, goal, history)
        result = await self.execute(device_id, decision["name"], decision.get("input", {}))

        return {
            "action": decision["name"],
            "args": decision.get("input", {}),
            "success": result.success,
            "message": result.message,
            "elements_seen": len(perception.get("elements", [])),
            "ocr_available": perception.get("ocr_available", False),
        }

    async def run(
        self, device_id: str, goal: Optional[str] = None, max_steps: int = 10
    ) -> dict:
        """Run up to ``max_steps`` decision cycles toward ``goal``."""
        goal = goal or DEFAULT_GOAL
        history: list[str] = []
        steps: list[dict] = []

        for _ in range(max_steps):
            entry = await self.step(device_id, goal, history)
            steps.append(entry)
            action = entry.get("action")
            summary = f"{action}({entry.get('args') or ''}) -> {'ok' if entry['success'] else 'fail'}: {entry['message']}"
            history.append(summary)
            if action == "stop":
                break

        return {"goal": goal, "steps": steps, "count": len(steps)}

    # ============================================================ campaign ====
    # A long-running, niche-aware loop: classify each video against the niche,
    # skip irrelevant ones fast, watch relevant ones for a human-random time,
    # and engage (like/save/comment) with randomised, varied behaviour.

    def _plan_tool(self) -> list[dict]:
        return [
            {
                "name": "plan_video",
                "description": (
                    f"Report whether the current TikTok video is relevant to the "
                    f"{NICHE['name']} niche and, if so, a fresh personal comment."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "on_feed": {
                            "type": "boolean",
                            "description": "TRUE whenever a single full-screen vertical video is PLAYING with the like/comment/share icons on the right -- a video you could swipe up to skip -- REGARDLESS of its topic, and whether it's the For You ('Für dich') or Following ('Gefolgt') tab. FALSE only when it is clearly NOT a playing video: a search box or grid of thumbnails, a profile page, an open comment/share sheet, the inbox/DMs, settings, or a menu/dialog. Do NOT set false just because the video is off-topic.",
                        },
                        "screen": {
                            "type": "string",
                            "description": "A short plain-language description of what is actually on screen right now (e.g. 'For You video about baking', 'profile page of @user, Reposts tab', 'comment sheet open').",
                        },
                        "post_type": {
                            "type": "string",
                            "enum": ["video", "photo_slideshow", "none"],
                            "description": "When on_feed: 'photo_slideshow' if the post is a photo carousel (multiple images with small slide-indicator dots near the bottom-centre, sometimes a slide counter like 1/5), otherwise 'video'. 'none' when not on a feed post.",
                        },
                        "relevant": {
                            "type": "boolean",
                            "description": "Only when on_feed: is the video's TOPIC relevant to the niche? An off-topic playing video is on_feed=true, relevant=false (we'll swipe past it).",
                        },
                        "reason": {
                            "type": "string",
                            "description": "A few words on the relevance decision.",
                        },
                        "language": {
                            "type": "string",
                            "description": "The primary language of the video's speech, caption, and on-screen text, as an English name (e.g. 'German', 'English', 'Polish', 'Spanish'). Your best guess from what you can read/see.",
                        },
                        "comment": {
                            "type": "string",
                            "description": "If on_feed and relevant, a fresh, human, personal-experience comment tailored to THIS video that subtly promotes the product. Leave empty otherwise.",
                        },
                    },
                    "required": ["on_feed", "screen", "relevant", "reason"],
                },
            }
        ]

    def _plan_system(self, used_comments: list[str]) -> str:
        parts = [
            f"You are curating a TikTok For You feed for the {NICHE['name']} niche.",
            "\nFIRST, decide on_feed. Set on_feed=TRUE whenever a single "
            "full-screen vertical video is PLAYING with the like/comment/share "
            "icons stacked on the right -- a video you could swipe up to skip. "
            "This is TRUE for both the 'Für dich' (For You) and 'Gefolgt' "
            "(Following) tabs, and TRUE no matter what the video is about. Do NOT "
            "set on_feed=false just because the video is off-topic -- that is what "
            "'relevant' is for; an off-topic video is on_feed=TRUE with "
            "relevant=FALSE and we will simply swipe past it.\n"
            "Set on_feed=FALSE ONLY when it is clearly not a playing full-screen "
            "video: a search box or a grid of video thumbnails (search results / "
            "explore), a profile page (avatar + follower counts + a video grid), "
            "an open comment or share sheet, the inbox/DMs, settings, a livestream "
            "chat, or any menu/dialog. Note: a Follow button, a like/comment count, "
            "and a @username appear on NORMAL feed videos too -- they do NOT mean "
            "it's a profile page. A profile page specifically shows a GRID of video "
            "thumbnails and tabs like Videos/Reposts/Liked. A photo SLIDESHOW post "
            "(a carousel of images with small slide-indicator dots near the "
            "bottom-centre) is STILL on_feed=TRUE -- set post_type='photo_slideshow' "
            "for it. Always fill in 'screen' with what you see.",
            f"\n{NICHE['name']} is: {NICHE['description']}",
            "\nA video is RELEVANT if it relates to any of: "
            + "; ".join(NICHE["relevant_topics"])
            + ".",
            "It is NOT relevant if it's, e.g.: " + "; ".join(NICHE["not_relevant"]) + ".",
            "Judge relevance by the video's TOPIC, not by whether the person has "
            "visible acne or skin marks -- someone with acne talking about moving, "
            "cooking, or lifestyle is NOT relevant.",
            "Be reasonably strict -- when in doubt, mark it not relevant so we skip it.",
            "\nAlways report the video's primary 'language' (English name).",
            "\nIf relevant, also write a comment. " + NICHE["comment_style"],
            f"Always spell the product name exactly as '{NICHE['name']}' -- never "
            "misspell it.",
            "Match the vibe of these examples but DO NOT copy them -- write something "
            "new that fits THIS specific video:\n- " + "\n- ".join(NICHE["comment_examples"]),
        ]
        if used_comments:
            parts.append(
                "\nYou have ALREADY posted these comments this session -- write "
                "something clearly different:\n- " + "\n- ".join(used_comments)
            )
        parts.append("\nCall plan_video with your decision.")
        return "\n".join(parts)

    async def plan_video(self, perception: dict, used_comments: list[str]) -> dict:
        """One Claude call: relevance + (if relevant) a fresh niche comment."""
        client = self.controller.ai.client

        elements = perception.get("elements", [])
        if elements:
            screen_text = "On-screen text (OCR):\n" + "\n".join(f"- {e['text']}" for e in elements)
        elif perception.get("ocr_available"):
            screen_text = "No text detected on screen."
        else:
            screen_text = "OCR unavailable; judge from the image."

        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": perception["screenshot_b64"],
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": f"{screen_text}\n\nClassify this video for the niche and, "
                "if relevant, write a fresh comment.",
            }
        )

        resp = await client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=500,
            system=self._plan_system(used_comments),
            tools=self._plan_tool(),
            tool_choice={"type": "tool", "name": "plan_video"},
            messages=[{"role": "user", "content": content}],
        )
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input or {})
                return {
                    "on_feed": bool(data.get("on_feed")),
                    "screen": (data.get("screen") or "").strip(),
                    "post_type": data.get("post_type") or "",
                    "relevant": bool(data.get("relevant")),
                    "reason": data.get("reason", ""),
                    "language": (data.get("language") or "").strip(),
                    "comment": (data.get("comment") or "").strip(),
                }
        return {"on_feed": False, "screen": "unknown", "post_type": "", "relevant": False,
                "reason": "no plan returned", "language": "", "comment": ""}

    @staticmethod
    def _language_matches(detected: str, target: str) -> bool:
        """Whether the detected content language matches the campaign target.
        Empty target = allow any; empty detection = don't over-filter."""
        if not target:
            return True
        d, t = detected.lower().strip(), target.lower().strip()
        if not d:
            return True
        alias = [
            {"german", "deutsch", "de"}, {"english", "en"}, {"polish", "polnisch", "pl"},
            {"spanish", "espanol", "español", "es"}, {"french", "francais", "français", "fr"},
            {"italian", "italiano", "it"}, {"portuguese", "portugues", "pt"},
            {"dutch", "nederlands", "nl"}, {"turkish", "türkisch", "tr"},
        ]
        for group in alias:
            if t in group and d in group:
                return True
        return d == t or d.startswith(t) or t.startswith(d)

    # ------------------------------------------------ UI exploration ---------
    # Interactive mapping: Claude looks at the current screen and reports the
    # controls it sees with pixel tap points, which the user verifies/corrects
    # and saves into the UI map. This is how we learn THIS phone's TikTok layout
    # instead of guessing fractional coordinates.

    DESCRIBE_TOOL = [
        {
            "name": "describe_screen",
            "description": "Report the TikTok screen type and every interactive UI control visible, each with a pixel tap point.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "screen_type": {
                        "type": "string",
                        "description": "One of: for_you_feed, slideshow_feed (a photo carousel post with slide-indicator dots), following_feed, profile, search_input, search_results, explore, comments_sheet, share_sheet, inbox_dm, live, settings, other.",
                    },
                    "description": {"type": "string", "description": "One line on what this screen is."},
                    "elements": {
                        "type": "array",
                        "description": "Each interactive control visible.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Canonical snake_case name, e.g. like_button, comment_button, share_button, save_button, repost_button, profile_avatar, follow_button, for_you_tab, following_tab, live_tab, search_icon, back_arrow, close_button, home_tab, friends_tab, inbox_tab, profile_tab, comment_input, post_button, caption, slide_indicator (the dots showing which slide of a photo slideshow you're on).",
                                },
                                "x": {"type": "integer", "description": "x pixel of the tap point."},
                                "y": {"type": "integer", "description": "y pixel of the tap point."},
                                "kind": {"type": "string", "enum": ["icon", "text", "tab", "button", "other"]},
                            },
                            "required": ["label", "x", "y"],
                        },
                    },
                },
                "required": ["screen_type", "description", "elements"],
            },
        }
    ]

    async def analyze_ui(self, device_id: str) -> dict:
        """Vision + OCR pass that maps the current screen's controls to pixels."""
        perception = await self.perceive(device_id)
        if perception.get("error"):
            return {"error": perception["error"]}

        device = await self.controller.get_device(device_id)
        w = (device.imgw or device.width) if device else 0
        h = (device.imgh or device.height) if device else 0

        elements = perception.get("elements", [])
        ocr_text = (
            "OCR text elements as 'text @ [x,y]' (reuse these exact coordinates "
            "for any labelled control):\n"
            + "\n".join(f"- {e['text']} @ {e['centre']}" for e in elements)
            if elements else "No OCR text available; estimate positions from the image."
        )
        system = (
            "You are mapping the TikTok app's interface on a phone so an "
            f"automation can navigate reliably. The screenshot is {w} pixels wide "
            f"and {h} pixels tall; give every coordinate in that pixel space "
            "(origin top-left). Identify the screen_type and list every "
            "interactive control you can see, each with a precise tap point. For "
            "controls that have OCR text, reuse that exact [x,y]. For icons "
            "(heart, speech bubble, arrow, avatar, tab-bar icons) estimate the "
            "centre of the tappable icon as accurately as you can. Use canonical "
            "snake_case labels. Call describe_screen."
        )
        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": perception["screenshot_b64"]},
                }
            )
        content.append({"type": "text", "text": f"{ocr_text}\n\nMap this screen."})

        client = self.controller.ai.client
        resp = await client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=1500,
            system=system,
            tools=self.DESCRIBE_TOOL,
            tool_choice={"type": "tool", "name": "describe_screen"},
            messages=[{"role": "user", "content": content}],
        )
        result = {"screen_type": "other", "description": "", "elements": []}
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input or {})
                result["screen_type"] = data.get("screen_type", "other")
                result["description"] = data.get("description", "")
                result["elements"] = data.get("elements", [])
        result["screenshot"] = perception.get("screenshot_b64")
        result["imgw"] = w
        result["imgh"] = h
        return result

    # ------------------------------------------- per-frame icon locating -----
    # TikTok's right-hand action rail (avatar / like / comment / bookmark /
    # share) SLIDES up or down depending on what's overlaid at the bottom -- a
    # "search this video" bar, an ad CTA, a tall caption, a pinned comment. A
    # fixed pixel from the UI map is right on one video and lands on the wrong
    # icon on the next. So before engaging we locate the rail on THIS frame with
    # vision; the whole rail moves together, so one call places every icon.

    LOCATE_TOOL = [
        {
            "name": "locate_controls",
            "description": (
                "Report the exact pixel tap point of each action-rail control "
                "visible on the current TikTok video."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "like_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the HEART / like icon. Omit if not visible."},
                    "comment_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the SPEECH-BUBBLE / comment icon. Omit if not visible."},
                    "save_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the BOOKMARK / save icon. Omit if not visible."},
                    "share_button": {"type": "array", "items": {"type": "integer"},
                        "description": "[x, y] centre of the SHARE / arrow icon. Omit if not visible."},
                },
            },
        }
    ]

    @staticmethod
    def _is_count(text: str) -> bool:
        """True for a rail count like '1303', '54', '12.3K', '1,2Mio', '1.2万'."""
        import re
        t = (text or "").strip()
        return bool(re.match(r"^[\d][\d.,]*\s*(?:[KkMm]|万|Mio|Tsd)?\.?$", t))

    def _ocr_rail_points(self, perception: dict, w: int, h: int) -> dict:
        """Anchor rail icons to the OCR rail as a regular 4-slot grid.

        The rail is 4 evenly-spaced slots below the avatar -- like, comment,
        save, share -- and each icon sits just above its count NUMBER. OCR gives
        those numbers' exact centres. But a slot may show a WORD label (Teilen,
        Speichern) instead of a number, or a count may be missing, so we don't
        just map the detected numbers 1:1. Instead we:
          1. find the follow "+" (directly above the like slot) and the numeric
             counts, all on the right edge;
          2. derive the slot SPACING (default 107 at 608x1080) and the like-slot
             y (first count below the "+", else the topmost count);
          3. lay a 4-slot grid from there, snapping each slot to a real detected
             count when one is nearby (pixel-exact) and extrapolating otherwise.
        This yields all four icons even when some slots have word labels.
        """
        if not w or not h:
            return {}
        plus_y: Optional[int] = None
        counts: list[tuple[int, int]] = []
        word_slot: dict[int, int] = {}  # slot index (2=save, 3=share) -> y
        for e in perception.get("elements", []):
            cx, cy = e["centre"]
            if not (cx > w * 0.85 and h * 0.12 < cy < h * 0.90):
                continue
            raw = (e.get("text") or "").strip()
            t = raw.lower()
            if raw in ("+", "＋"):        # follow badge (ASCII or full-width +)
                plus_y = int(cy)
            elif self._is_count(raw):
                counts.append((int(cy), int(cx)))
            elif "teilen" in t or "share" in t or "senden" in t:
                word_slot[3] = int(cy)   # share is the bottom rail slot
            elif "speicher" in t or "favorit" in t or "merken" in t:
                word_slot[2] = int(cy)   # save is the third rail slot
        counts.sort()
        if not counts and plus_y is None and not word_slot:
            return {}

        railx = int(sum(cx for _, cx in counts) / len(counts)) if counts else int(w * 0.917)
        if not railx:
            railx = int(w * 0.917)

        # Slot spacing: the MEDIAN consecutive gap (robust to a spurious element
        # above/below the rail and to a missing count doubling one gap), and only
        # if it lands in the plausible rail range; else the known default.
        spacing = 0
        if len(counts) >= 2:
            gaps = sorted(b[0] - a[0] for a, b in zip(counts, counts[1:]))
            med = gaps[len(gaps) // 2]
            if 90 <= med <= 125:
                spacing = med
        if not spacing:
            spacing = 107
        offset = int(spacing * self.config.rail_icon_offset_factor)

        # Like-slot y (the y of the like COUNT). Anchor priority, most reliable
        # first: the follow "+" (directly above the like slot) -> a label word
        # like Teilen/Speichern (marks an ABSOLUTE slot, so it disambiguates
        # which count is which) -> the topmost count (assumes like has a count).
        like_y: Optional[int] = None
        if plus_y is not None:
            below = [cy for cy, _ in counts if cy > plus_y + spacing * 0.4]
            like_y = min(below) if below else int(plus_y + spacing)
        elif word_slot:
            slot = max(word_slot)  # prefer share (3) over save (2)
            like_y = int(word_slot[slot] - slot * spacing)
        elif counts:
            like_y = counts[0][0]
        if like_y is None:
            return {}

        labels = ["like_button", "comment_button", "save_button", "share_button"]
        points: dict[str, tuple[int, int]] = {}
        for i, label in enumerate(labels):
            slot_y = like_y + i * spacing
            near = [cy for cy, _ in counts if abs(cy - slot_y) <= spacing * 0.4]
            if near:  # snap to the real count for pixel accuracy
                slot_y = min(near, key=lambda cy: abs(cy - slot_y))
            icon_y = slot_y - offset
            if h * 0.10 < icon_y < h * 0.92:
                points[label] = (railx, int(icon_y))
        return points

    async def locate_actions(
        self, device_id: str, perception: Optional[dict] = None
    ) -> dict:
        """Locate the right-rail icons on the current frame.

        Prefers OCR count anchoring (pixel-exact); falls back to a vision guess
        only if OCR gave us no rail. Returns ``{label: (x, y)}`` for whichever of
        like/comment/save/share are found; ``{}`` on failure, so callers can then
        fall back to the UI map.
        """
        if perception is None:
            perception = await self.perceive(device_id)

        device = await self.controller.get_device(device_id)
        w = (device.imgw or device.width) if device else 0
        h = (device.imgh or device.height) if device else 0

        # 1) OCR anchoring -- exact, shift-proof.
        ocr_pts = self._ocr_rail_points(perception, w, h)
        if ocr_pts.get("like_button"):
            self._last_locate_source = "ocr"
            return ocr_pts

        # 2) Vision fallback (approximate) when OCR has no rail counts.
        shot = perception.get("screenshot_b64")
        if not shot:
            return {}
        self._last_locate_source = "vision"

        elements = perception.get("elements", [])
        ocr_hint = (
            "OCR text with positions (the small NUMBER under each icon is its "
            "count -- the icon sits just ABOVE its count):\n"
            + "\n".join(f"- {e['text']} @ {e['centre']}" for e in elements)
            if elements else "No OCR text available; read the icons from the image."
        )
        system = (
            f"The screenshot is {w} pixels wide and {h} pixels tall; give every "
            "coordinate in that exact pixel space (origin top-left). Find the "
            "vertical ACTION RAIL on the RIGHT edge of a TikTok video: from top "
            "to bottom it is the round profile avatar, then the HEART (like), the "
            "SPEECH BUBBLE (comment), the BOOKMARK (save), and the SHARE arrow. "
            "This rail slides up or down between videos depending on bottom "
            "overlays, so read the real position from THIS image -- do not assume "
            "fixed spots. Return the centre [x, y] of each icon you can clearly "
            "see; omit any that are not visible. Call locate_controls."
        )
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": shot}},
            {"type": "text", "text": f"{ocr_hint}\n\nLocate the action-rail icons."},
        ]
        client = self.controller.ai.client
        try:
            resp = await client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=400,
                system=system,
                tools=self.LOCATE_TOOL,
                tool_choice={"type": "tool", "name": "locate_controls"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001 - never let locating break a step
            logger.warning("locate_actions failed: %s", exc)
            return {}

        points: dict[str, tuple[int, int]] = {}
        for block in resp.content:
            if block.type == "tool_use":
                data = dict(block.input or {})
                for label in ("like_button", "comment_button", "save_button", "share_button"):
                    xy = data.get(label)
                    if isinstance(xy, (list, tuple)) and len(xy) == 2:
                        x, y = int(xy[0]), int(xy[1])
                        # Sanity: inside the frame and in the right-hand rail zone.
                        if w and h and 0 <= x <= w and 0 <= y <= h and x > w * 0.6:
                            points[label] = (x, y)
        return points

    async def locate_one(
        self, screenshot_b64: str, w: int, h: int, what: str
    ) -> Optional[tuple[int, int]]:
        """Vision-locate a single named control on an arbitrary screenshot.

        Used to calibrate off-app screens (e.g. Control Center for mirror
        re-cast). Returns (x, y) in the imgw x imgh space, or None if not found.
        """
        if not screenshot_b64:
            return None
        tool = [{
            "name": "locate",
            "description": f"Report the pixel tap point of {what}.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["found"],
            },
        }]
        system = (
            f"The screenshot is {w} pixels wide and {h} tall (origin top-left). "
            f"Find {what} and give its centre tap point in that exact pixel space. "
            "Call locate; set found=false if it is not visible."
        )
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}},
            {"type": "text", "text": f"Locate: {what}."},
        ]
        try:
            resp = await self.controller.ai.client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=200,
                system=system,
                tools=tool,
                tool_choice={"type": "tool", "name": "locate"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("locate_one failed: %s", exc)
            return None
        for block in resp.content:
            if block.type == "tool_use":
                d = dict(block.input or {})
                if d.get("found") and d.get("x") is not None and d.get("y") is not None:
                    x, y = int(d["x"]), int(d["y"])
                    if 0 <= x <= w and 0 <= y <= h:
                        return (x, y)
        return None

    # ------------------------------------------------ vision recovery --------
    # Blind fixed-coordinate taps are dangerous (top-left = LIVE, wrong bottom
    # tab = DMs/profile), so recovery is vision-guided: Claude looks at the stuck
    # screen and picks ONE navigation control to escape toward the feed.

    RECOVER_TOOL = [
        {
            "name": "recover",
            "description": "Choose ONE action to move back toward the TikTok For You feed from the current (non-feed) screen.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["tap", "hardware_back", "swipe_down"],
                        "description": "tap = tap a specific navigation control (a top-left back arrow, an X/Close/Cancel button, or the Home/For-You tab in the bottom bar). hardware_back = press the phone's Back. swipe_down = pull down to dismiss an open comment/share sheet.",
                    },
                    "x": {"type": "integer", "description": "x pixel to tap (method=tap only), in the screenshot's pixel space."},
                    "y": {"type": "integer", "description": "y pixel to tap (method=tap only)."},
                    "note": {"type": "string", "description": "Briefly, what you are tapping or doing."},
                },
                "required": ["method", "note"],
            },
        }
    ]

    async def recover(self, device_id: str, perception: dict) -> dict:
        """Ask Claude how to escape a non-feed screen toward the For You feed."""
        client = self.controller.ai.client
        device = await self.controller.get_device(device_id)
        w = (device.imgw or device.width) if device else 0
        h = (device.imgh or device.height) if device else 0

        elements = perception.get("elements", [])
        screen_text = (
            "On-screen text elements as 'text @ [x,y]' (you can tap any of these "
            "coordinates exactly):\n"
            + "\n".join(f"- {e['text']} @ {e['centre']}" for e in elements)
            if elements else "No OCR text; judge positions from the image."
        )
        system = (
            "You are helping an automation get back to the TikTok For You feed (a "
            f"single full-screen video with like/comment/share on the right). The "
            f"screenshot is {w} pixels wide and {h} pixels tall. Look at the screen "
            "and choose ONE action to move toward that feed.\n"
            "- 'tap' a clear navigation control and give its pixel (x,y). If the "
            "control has an OCR text entry above (e.g. Cancel/Abbrechen/Back), reuse "
            "its exact [x,y]. Otherwise estimate from the image (e.g. a top-left "
            "back arrow, an X/Close button, or the Home/For-You tab in the bottom "
            "bar).\n"
            "- 'swipe_down' to dismiss an open comment or share sheet.\n"
            "- 'hardware_back' if there is no obvious control.\n"
            "NEVER tap the LIVE button, a profile, a video, a link, a follow button, "
            "or any content -- ONLY a back/close/navigation control. Call recover."
        )
        content: list[dict] = []
        if perception.get("screenshot_b64"):
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": perception["screenshot_b64"]},
                }
            )
        content.append({"type": "text", "text": f"{screen_text}\n\nChoose the recovery action."})

        try:
            resp = await client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=300,
                system=system,
                tools=self.RECOVER_TOOL,
                tool_choice={"type": "tool", "name": "recover"},
                messages=[{"role": "user", "content": content}],
            )
            for block in resp.content:
                if block.type == "tool_use":
                    return dict(block.input or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("recover decision failed: %s", exc)
        return {"method": "hardware_back", "note": "fallback"}

    async def apply_recovery(self, device_id: str, move: dict) -> None:
        """Execute a recovery move chosen by :meth:`recover`."""
        method = move.get("method")
        try:
            if method == "tap" and move.get("x") is not None and move.get("y") is not None:
                await self.controller.api.click(device_id, int(move["x"]), int(move["y"]))
            elif method == "swipe_down":
                await self.controller.api.swipe(device_id, "down", 0.6)
            else:  # hardware_back / fallback
                await self.controller.api.send_key(device_id, fn_key="TAB+b")
        except (IMouseError, ValueError, TypeError) as exc:
            logger.warning("apply_recovery failed: %s", exc)
        await asyncio.sleep(self.config.action_delay)

    # ------------------------------------------------ human-like timing ------
    def _rand(self, lo: float, hi: float) -> float:
        return random.uniform(lo, hi)

    def _watch_relevant(self) -> float:
        c = self.config.campaign
        if random.random() < c["watch_relevant_long_chance"]:
            return self._rand(*c["watch_relevant_long"])
        return self._rand(*c["watch_relevant"])

    def _watch_irrelevant(self) -> float:
        c = self.config.campaign
        if random.random() < c["watch_irrelevant_long_chance"]:
            return self._rand(*c["watch_irrelevant_long"])
        return self._rand(*c["watch_irrelevant"])

    def _pick_actions(self, has_comment: bool, comment_allowed: bool) -> list[str]:
        c = self.config.campaign
        actions = []
        if random.random() < c["like_probability"]:
            actions.append("like")
        if random.random() < c["save_probability"]:
            actions.append("save")
        if has_comment and comment_allowed and random.random() < c["comment_probability"]:
            actions.append("comment")
        random.shuffle(actions)  # vary the order too
        return actions

    # TikTok UI chrome that appears on EVERY feed frame -- must be excluded from
    # the post fingerprint or it dominates and makes different videos look alike
    # (and the clock/nav jitter makes the same video look different).
    UI_STOP = {
        "community", "entdecken", "furdich", "dich", "gefolgt", "folgen", "folgeich",
        "home", "posteingang", "profil", "shop", "freunde", "jetzt", "live",
        "suchen", "following", "foryou", "friends", "inbox", "profile", "discover",
        "simfehlt", "gefolgtshop", "shopfurdich", "communitygefolgt", "mehr",
    }

    @classmethod
    def _post_words(cls, perception: dict) -> set:
        """Caption/username words identifying the current POST -- with the TikTok
        nav bar, status bar, and clock stripped out. Compared fuzzily, this
        reliably tells one video from another despite OCR jitter."""
        words = set()
        for e in perception.get("elements", []):
            for raw in (e.get("text") or "").lower().split():
                if any(ch.isdigit() for ch in raw):
                    continue  # drop clock (943am), counts, times
                w = "".join(ch for ch in raw if ch.isalnum())
                if len(w) >= 4 and w not in cls.UI_STOP:
                    words.add(w)
        return words

    @staticmethod
    def _same_post(a: set, b: set) -> bool:
        """True if two word sets describe the same post (fuzzy: shared caption
        words), tolerating OCR noise between captures."""
        if not a or not b:
            return False
        inter = len(a & b)
        return inter >= 3 and inter / len(a | b) >= 0.45

    async def _advance_feed(self, device_id: str, current_words: set) -> None:
        """Advance to the next post -- ONE firm swipe, then confirm.

        Rapid repeat swipes land mid-transition and glitch the feed back to the
        same video, so we never fire two swipes close together: after a swipe we
        wait for a SETTLED frame (mirror caught up), and only if it's still the
        same post do we try once more. The cross-cycle dedup catches anything
        that still slips through, without ever re-reacting."""
        api = self.controller.api
        # Try gestures in order, VERIFYING each against the clean caption (no UI
        # chrome), settling fully between attempts so none land mid-transition.
        # Whatever actually moves the feed wins; we stop as soon as it changes.
        attempts = [
            lambda: api.swipe(device_id, "up", 0.75),
            lambda: api.swipe(device_id, "up", 0.9),
            lambda: api.wheel(device_id, "down", 0.6, 5),
        ]
        for act in attempts:
            try:
                await act()
            except IMouseError:
                continue
            await asyncio.sleep(self.config.post_swipe_settle)
            perc = await self.settled_perception(device_id, tries=2)
            if perc.get("error"):
                self._campaign["next_perc"] = None
                return
            if not (current_words and self._same_post(self._post_words(perc), current_words)):
                # Advanced. Hand this already-settled frame to the next cycle so
                # it doesn't re-capture -- a big per-video latency saving.
                self._campaign["next_perc"] = perc
                return
        self._campaign["next_perc"] = None

    def _comment_allowed(self) -> bool:
        """Sparse, non-spammy commenting: only once the randomized cooldown has
        passed and under the per-run cap."""
        st, c = self._campaign, self.config.campaign
        if st["stats"]["comments"] >= c.get("comment_max_per_session", 8):
            return False
        if st["comment_gap"] <= 0:  # first time -- set an initial cooldown target
            st["comment_gap"] = random.randint(*c["comment_min_gap"])
        return st["videos_since_comment"] >= st["comment_gap"]

    async def _sleep(self, seconds: float) -> None:
        """Cancellable sleep: wakes early when a stop is requested."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end and not self._campaign["stop"]:
            await asyncio.sleep(min(0.5, end - time.monotonic()))

    # -------------------------------------------------- slideshow browsing ---
    @staticmethod
    def _slide_count(perception: dict) -> Optional[int]:
        """Read the total slide count from an 'N/M' indicator in the OCR."""
        import re
        for el in perception.get("elements", []):
            m = re.search(r"\b\d+\s*/\s*(\d+)\b", el.get("text", ""))
            if m:
                total = int(m.group(1))
                if 2 <= total <= 20:
                    return total
        return None

    async def _browse_slideshow(self, device_id: str, perception: dict, view_all: bool) -> int:
        """Swipe through a photo slideshow's slides. All of them when ``view_all``
        (we're about to comment), otherwise a random subset. Returns slides viewed.
        """
        c = self.config.campaign
        direction = c.get("slideshow_swipe_direction", "right")
        total = self._slide_count(perception)
        if view_all:
            count = (total - 1) if total else c["slideshow_max_slides"]
        else:
            upper = (total - 1) if total else 4
            count = random.randint(1, max(1, upper))
        count = min(count, c["slideshow_max_slides"])

        viewed = 0
        for _ in range(count):
            if self._campaign["stop"]:
                break
            try:
                await self.controller.api.swipe(device_id, direction, 0.6)
            except IMouseError:
                break
            viewed += 1
            await self._sleep(self._rand(*c["slide_dwell"]))
        return viewed

    # ---------------------------------------------------- per-video flow -----
    async def _campaign_video(self, device_id: str) -> dict:
        st = self._campaign
        c = self.config.campaign

        # Reuse the settled frame the last advance already captured, if any --
        # otherwise wait for a fresh settled frame (the mirror lags the phone, so
        # a stale transitional capture must not drive the decision).
        perception = st.get("next_perc")
        st["next_perc"] = None
        if perception is None:
            perception = await self.settled_perception(device_id)
        st["last_screenshot"] = perception.get("screenshot_b64")  # live view for UI
        if perception.get("error"):
            st["stats"]["errors"] += 1
            st["error_streak"] += 1
            # code 25 = the AirPlay mirror dropped (control still works over USB,
            # so checking device online-state is useless -- a screenshot is the
            # only real test). Try to re-cast the mirror from the phone side.
            mirror_down = perception.get("error_code") == 25
            entry = {"on_feed": None, "relevant": None, "actions": [],
                     "reason": perception["error"], "watch": 0,
                     "screen": "mirror dropped" if mirror_down else "screenshot failed"}
            recovered = False
            if mirror_down:
                try:
                    recovered = await self.controller.recover_mirror(device_id)
                except Exception:  # noqa: BLE001
                    recovered = False
            if recovered:
                st["error_streak"] = 0
                entry["actions"].append("re-cast mirror OK")
                return entry
            # Circuit breaker: never hammer a dead mirror unattended. Stop after a
            # short streak of unrecovered errors with a clear status.
            if st["error_streak"] >= 6:
                st["stop"] = True
                why = ("mirror down; re-cast unavailable (not calibrated) or failed"
                       if mirror_down else "screenshot errors not clearing")
                entry["actions"].append(f"STOPPED ({why})")
            else:
                await self._sleep(min(2.0 + st["error_streak"], 8.0))  # backoff
            return entry
        st["error_streak"] = 0

        try:
            plan = await self.plan_video(perception, st["used_comments"][-6:])
        except Exception as exc:  # noqa: BLE001
            st["stats"]["errors"] += 1
            await self._sleep(2.0)
            return {"on_feed": None, "screen": "plan failed", "relevant": None,
                    "reason": f"plan failed: {exc}", "actions": [], "watch": 0}

        # Off the feed (profile, sheet, search, …): recover, don't engage.
        if not plan["on_feed"]:
            st["stats"]["recovered"] += 1
            st["off_feed_streak"] += 1
            move = await self.recover(device_id, perception)
            await self.apply_recovery(device_id, move)
            await self._sleep(self._rand(1.5, 3.0))
            entry = {
                "on_feed": False, "screen": plan["screen"], "relevant": None,
                "reason": plan["reason"],
                "actions": [f"recover:{move.get('method', '?')} (try {st['off_feed_streak']})"],
                "recovery": move.get("note", ""), "watch": 0,
            }
            # Safety net: never spiral unsupervised. Stop after too many failures.
            if st["off_feed_streak"] >= 12:
                st["stop"] = True
                entry["actions"].append("STOPPED (stuck off-feed)")
            return entry

        st["off_feed_streak"] = 0
        st["stats"]["videos"] += 1

        # Identify the post (caption words) and check if we've already handled it
        # this session -- if so, never engage it again (that caused the re-liking
        # and unliking), just move on.
        words = self._post_words(perception)
        already = any(self._same_post(words, seen) for seen in st["engaged_posts"])

        # Language gate: only engage content in the configured language.
        target_lang = NICHE.get("language")
        off_language = bool(target_lang) and not self._language_matches(
            plan.get("language", ""), target_lang
        )

        if not plan["relevant"] or already or off_language:
            fast = (not plan["relevant"]) or off_language
            watch = self._watch_irrelevant() if fast else self._rand(0.8, 2.0)
            await self._sleep(watch)
            await self._advance_feed(device_id, words)
            reason = ("off-language: " + (plan.get("language") or "?")) if off_language else (
                "already engaged" if already else "irrelevant")
            actions = [f"skip ({reason})"] if (already or off_language) else []
            return {"on_feed": True, "screen": plan["screen"], "post_type": plan.get("post_type", ""),
                    "relevant": bool(plan["relevant"]), "reason": plan["reason"],
                    "actions": actions, "watch": round(watch, 1)}

        # New relevant video: mark it engaged NOW (before acting) so we never
        # double-handle it, then decide reactions ONCE, do them, and always
        # advance to the next post at the end.
        st["stats"]["relevant"] += 1
        st["videos_since_comment"] += 1
        if words:
            st["engaged_posts"].append(words)
            st["engaged_posts"] = st["engaged_posts"][-150:]
        watch = self._watch_relevant()
        await self._sleep(watch * self._rand(0.3, 0.6))

        # Decide engagement up front so a slideshow can browse to the end when
        # we're going to comment (per requirement), or a random subset otherwise.
        planned = self._pick_actions(
            has_comment=bool(plan["comment"]),
            comment_allowed=self._comment_allowed(),
        )
        slides_viewed = 0
        if plan.get("post_type") == "photo_slideshow":
            slides_viewed = await self._browse_slideshow(
                device_id, perception, view_all=("comment" in planned)
            )

        # Locate the action rail on this frame once (it shifts per video); reuse
        # the plan's screenshot so there's no extra capture. All engagement taps
        # for this video use these live coordinates, falling back to the map.
        pts = {}
        if planned:
            pts = await self.locate_actions(device_id, perception)

        done = []
        for action in planned:
            if st["stop"]:
                break
            if action == "like":
                if (await self.controller.like_video(
                    device_id, point=pts.get("like_button")
                )).success:
                    st["stats"]["likes"] += 1
                    done.append("like")
            elif action == "save":
                if (await self.controller.save_video(
                    device_id, point=pts.get("save_button")
                )).success:
                    st["stats"]["saves"] += 1
                    done.append("save")
            elif action == "comment":
                res = await self.controller.comment_on_video(
                    device_id, comment=plan["comment"], use_ai=False,
                    comment_button_point=pts.get("comment_button"),
                )
                if res.success:
                    st["stats"]["comments"] += 1
                    st["used_comments"].append(plan["comment"])
                    done.append("comment")
                    # Reset the cooldown to a fresh randomized gap so comments
                    # stay spread out and never cluster into something spammy.
                    st["videos_since_comment"] = 0
                    st["comment_gap"] = random.randint(*c["comment_min_gap"])
            await self._sleep(self._rand(*c["between_actions"]))

        # Linger a touch, then ALWAYS advance to the next post -- verified, so a
        # swipe the feed ignores can't leave us re-processing this same video.
        await self._sleep(self._watch_relevant_remainder(watch))
        await self._advance_feed(device_id, words)
        return {
            "on_feed": True,
            "screen": plan["screen"],
            "post_type": plan.get("post_type", ""),
            "relevant": True,
            "reason": plan["reason"],
            "actions": (done + [f"viewed {slides_viewed} slides"]) if slides_viewed else done,
            "comment": plan["comment"] if "comment" in done else "",
            "watch": round(watch, 1),
        }

    def _watch_relevant_remainder(self, watch: float) -> float:
        # A little extra dwell after engaging, jittered.
        return max(0.0, self._rand(1.0, watch * 0.4))

    # -------------------------------------------------------- run / status ---
    async def campaign_loop(self, device_id: str, duration_seconds: int) -> dict:
        st = self._campaign = self._fresh_campaign_state()
        st.update(running=True, device_id=device_id, duration=duration_seconds)
        start = time.monotonic()
        logger.info("Campaign started on %s for %ss", device_id, duration_seconds)
        try:
            while (time.monotonic() - start) < duration_seconds and not st["stop"]:
                entry = await self._campaign_video(device_id)
                entry["t"] = round(time.monotonic() - start, 1)
                st["log"].append(entry)
                st["log"] = st["log"][-200:]
                st["elapsed"] = round(time.monotonic() - start, 1)
        finally:
            st["running"] = False
            st["elapsed"] = round(time.monotonic() - start, 1)
            logger.info("Campaign ended: %s", st["stats"])
        return {"stats": st["stats"], "videos": st["stats"]["videos"]}

    def request_stop(self) -> None:
        self._campaign["stop"] = True

    def campaign_status(self) -> dict:
        st = self._campaign
        remaining = max(0.0, st["duration"] - st["elapsed"]) if st["running"] else 0.0
        return {
            "running": st["running"],
            "device_id": st["device_id"],
            "duration": st["duration"],
            "elapsed": st["elapsed"],
            "remaining": round(remaining, 1),
            "stats": dict(st["stats"]),
            "recent": st["log"][-40:],
            "last_screenshot": st.get("last_screenshot"),
        }
