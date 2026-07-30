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
        }

    # --------------------------------------------------------- perceive ------
    async def perceive(self, device_id: str) -> dict:
        """Capture the screen and OCR elements for one decision."""
        result: dict[str, Any] = {"screenshot_b64": None, "elements": [], "ocr_available": False}

        try:
            result["screenshot_b64"] = await self.controller.get_screenshot_b64(device_id)
        except IMouseError as exc:
            result["error"] = f"screenshot failed: {exc}"
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
        if name == "like":
            return await c.like_video(device_id)
        if name == "save":
            return await c.save_video(device_id)
        if name == "repost":
            return await c.repost_video(device_id)
        if name == "return_to_feed":
            return await c.return_to_feed(device_id)
        if name == "comment":
            text = args.get("text", "")
            if not text:
                return ActionResult.fail("comment called without text")
            return await c.comment_on_video(device_id, comment=text, use_ai=False)
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
                            "description": "True ONLY if this is a normal, full-screen For You video that's playing. False for a profile page, comments, share sheet, search, DMs, settings, an ad/sponsor screen, a login prompt, or anything else.",
                        },
                        "screen": {
                            "type": "string",
                            "description": "A short plain-language description of what is actually on screen right now (e.g. 'For You video about baking', 'profile page of @user, Reposts tab', 'comment sheet open').",
                        },
                        "relevant": {
                            "type": "boolean",
                            "description": "Only meaningful when on_feed is true: is the video's topic relevant to the niche?",
                        },
                        "reason": {
                            "type": "string",
                            "description": "A few words on the relevance decision.",
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
            "\nFIRST, look at the screen and set on_feed. The For You feed is a "
            "single full-screen video playing, with the action icons (like, "
            "comment, share) stacked on the right and a caption bottom-left. If "
            "you instead see a profile page (avatar + follower counts + a grid of "
            "videos, tabs like Videos/Reposts/Liked), an open comment or share "
            "sheet, search, DMs, settings, an ad, or a login prompt, set on_feed "
            "to FALSE -- we'll navigate back to the feed and you should NOT judge "
            "relevance or write a comment for those screens. Always fill in "
            "'screen' with what you actually see.",
            f"\n{NICHE['name']} is: {NICHE['description']}",
            "\nA video is RELEVANT if it relates to any of: "
            + "; ".join(NICHE["relevant_topics"])
            + ".",
            "It is NOT relevant if it's, e.g.: " + "; ".join(NICHE["not_relevant"]) + ".",
            "Be reasonably strict -- when in doubt, mark it not relevant so we skip it.",
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
                    "relevant": bool(data.get("relevant")),
                    "reason": data.get("reason", ""),
                    "comment": (data.get("comment") or "").strip(),
                }
        return {"on_feed": False, "screen": "unknown", "relevant": False, "reason": "no plan returned", "comment": ""}

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

    def _pick_actions(self, has_comment: bool) -> list[str]:
        c = self.config.campaign
        actions = []
        if random.random() < c["like_probability"]:
            actions.append("like")
        if random.random() < c["save_probability"]:
            actions.append("save")
        if has_comment and random.random() < c["comment_probability"]:
            actions.append("comment")
        random.shuffle(actions)  # vary the order too
        return actions

    async def _sleep(self, seconds: float) -> None:
        """Cancellable sleep: wakes early when a stop is requested."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end and not self._campaign["stop"]:
            await asyncio.sleep(min(0.5, end - time.monotonic()))

    # ---------------------------------------------------- per-video flow -----
    async def _campaign_video(self, device_id: str) -> dict:
        st = self._campaign
        c = self.config.campaign

        perception = await self.perceive(device_id)
        st["last_screenshot"] = perception.get("screenshot_b64")  # live view for UI
        if perception.get("error"):
            st["stats"]["errors"] += 1
            await self._sleep(2.0)
            return {"on_feed": None, "screen": "screenshot failed", "relevant": None,
                    "reason": perception["error"], "actions": [], "watch": 0}

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
            await self.controller.return_to_feed(device_id, attempt=st["off_feed_streak"] - 1)
            await self._sleep(self._rand(1.5, 3.0))
            return {"on_feed": False, "screen": plan["screen"], "relevant": None,
                    "reason": plan["reason"],
                    "actions": [f"return_to_feed (try {st['off_feed_streak']})"], "watch": 0}

        st["off_feed_streak"] = 0
        st["stats"]["videos"] += 1

        if not plan["relevant"]:
            watch = self._watch_irrelevant()
            await self._sleep(watch)
            await self.controller.swipe_next(device_id)
            return {"on_feed": True, "screen": plan["screen"], "relevant": False,
                    "reason": plan["reason"], "actions": [], "watch": round(watch, 1)}

        # Relevant: watch part of the time, engage, linger, then move on.
        st["stats"]["relevant"] += 1
        watch = self._watch_relevant()
        await self._sleep(watch * self._rand(0.3, 0.6))

        done = []
        for action in self._pick_actions(has_comment=bool(plan["comment"])):
            if st["stop"]:
                break
            if action == "like":
                if (await self.controller.like_video(device_id)).success:
                    st["stats"]["likes"] += 1
                    done.append("like")
            elif action == "save":
                if (await self.controller.save_video(device_id)).success:
                    st["stats"]["saves"] += 1
                    done.append("save")
            elif action == "comment":
                res = await self.controller.comment_on_video(
                    device_id, comment=plan["comment"], use_ai=False
                )
                if res.success:
                    st["stats"]["comments"] += 1
                    st["used_comments"].append(plan["comment"])
                    done.append("comment")
            await self._sleep(self._rand(*c["between_actions"]))

        await self._sleep(self._watch_relevant_remainder(watch))
        await self.controller.swipe_next(device_id)
        return {
            "on_feed": True,
            "screen": plan["screen"],
            "relevant": True,
            "reason": plan["reason"],
            "actions": done,
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
