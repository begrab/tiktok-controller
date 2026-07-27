"""AI-powered video understanding and comment generation using Claude.

Uses the Anthropic Messages API with vision: a JPEG screenshot of the phone
screen is sent to Claude, which reads the caption/hashtags/creator and drafts a
comment.

Cost notes from the handover: an image costs roughly ``(width * height) / 750``
tokens -- about 875 tokens for a 608x1080 screenshot. Each call here is
stateless (a single screenshot, no accumulated history), which is the cheap
path; we never re-send earlier screenshots. Prompt caching and a history cap are
the levers that matter if a conversational loop is ever added.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from anthropic import AsyncAnthropic

from config import Config

logger = logging.getLogger("ai_comments")


@dataclass
class VideoContext:
    """Context extracted from a TikTok video frame."""

    caption: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    username: Optional[str] = None
    ocr_text: Optional[str] = None
    screenshot_base64: Optional[str] = None


STYLE_PROMPTS = {
    "friendly": "warm, supportive, and genuinely engaged",
    "funny": "witty, humorous, with clever observations",
    "supportive": "encouraging, positive, and uplifting",
    "curious": "asking a thoughtful question, showing genuine interest",
    "impressed": "expressing admiration and appreciation",
    "relatable": "sharing a similar experience or feeling",
}

LENGTH_GUIDES = {
    "short": "Keep it to 1-2 sentences, very brief",
    "medium": "2-3 sentences with a bit more detail",
    "long": "3-5 sentences with full engagement",
}


class AICommentGenerator:
    """Generate context-aware comments with Claude."""

    def __init__(self, config: Config):
        self.config = config
        self._client: Optional[AsyncAnthropic] = None

    @property
    def client(self) -> AsyncAnthropic:
        if self._client is None:
            if not self.config.anthropic_api_key:
                raise ValueError("Anthropic API key not configured")
            self._client = AsyncAnthropic(api_key=self.config.anthropic_api_key)
        return self._client

    def set_api_key(self, api_key: str) -> None:
        self.config.anthropic_api_key = api_key
        self._client = None  # rebuild with the new key

    @staticmethod
    def _image_block(screenshot_base64: str) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": screenshot_base64,
            },
        }

    async def _message(self, system: str, content: list[dict], max_tokens: int = 400) -> str:
        resp = await self.client.messages.create(
            model=self.config.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        parts = [block.text for block in resp.content if block.type == "text"]
        return "".join(parts).strip()

    async def analyze_screenshot(self, screenshot_base64: str) -> dict:
        """Extract structured context from a screenshot."""
        system = (
            "You analyze a TikTok video screenshot and extract structured data. "
            "Respond with ONLY a JSON object (no markdown fences) with keys: "
            "caption (string), hashtags (array of strings), username (string), "
            "content_description (string), mood (string). Use empty string or "
            "empty array when unknown."
        )
        content = [
            self._image_block(screenshot_base64),
            {"type": "text", "text": "Extract the context from this TikTok screenshot."},
        ]
        raw = await self._message(system, content, max_tokens=500)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{") : raw.rfind("}") + 1]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("analyze_screenshot: non-JSON reply: %s", raw[:200])
            return {}

    async def generate_comment(
        self,
        context: VideoContext,
        style: str = "friendly",
        length: str = "short",
        include_emoji: bool = True,
        custom_instructions: Optional[str] = None,
    ) -> str:
        """Draft a single contextual comment."""
        emoji_rule = (
            "Include 1-2 relevant emojis." if include_emoji else "Do not use emojis."
        )
        extra = f"\nAdditional instructions: {custom_instructions}" if custom_instructions else ""
        system = (
            "You write authentic TikTok comments.\n"
            f"Voice: {STYLE_PROMPTS.get(style, STYLE_PROMPTS['friendly'])}.\n"
            f"Length: {LENGTH_GUIDES.get(length, LENGTH_GUIDES['short'])}.\n"
            f"{emoji_rule}\n"
            "The comment must feel genuinely human, be specific to this video, "
            "and match its energy. Avoid generic filler like 'Great video!' or "
            "'Love this!'. Do not be promotional or spammy."
            f"{extra}\n"
            "Output ONLY the comment text, nothing else."
        )

        parts = []
        if context.caption:
            parts.append(f"Caption: {context.caption}")
        if context.hashtags:
            parts.append(f"Hashtags: {', '.join(context.hashtags)}")
        if context.username:
            parts.append(f"Creator: {context.username}")
        if context.ocr_text:
            parts.append(f"On-screen text: {context.ocr_text}")
        context_str = "\n".join(parts) if parts else "No text context available."

        content: list[dict] = []
        if context.screenshot_base64:
            content.append(self._image_block(context.screenshot_base64))
        content.append(
            {"type": "text", "text": f"Write a comment for this TikTok:\n\n{context_str}"}
        )
        return await self._message(system, content, max_tokens=200)

    async def generate_multiple_comments(
        self,
        context: VideoContext,
        count: int = 3,
        styles: Optional[list[str]] = None,
    ) -> list[str]:
        """Draft several comment options across different styles."""
        if styles is None:
            styles = ["friendly", "funny", "supportive"]
        comments = []
        for i in range(count):
            comments.append(
                await self.generate_comment(context, style=styles[i % len(styles)])
            )
        return comments

    async def generate_post_caption(
        self,
        description: str,
        mood: str = "engaging",
        include_hashtags: bool = True,
        hashtag_count: int = 5,
    ) -> str:
        """Draft a caption for posting content."""
        hashtag_rule = (
            f"End with {hashtag_count} relevant trending hashtags."
            if include_hashtags
            else "Do not include hashtags."
        )
        system = (
            f"You are a TikTok creator writing a {mood} caption. It must be "
            "attention-grabbing, authentic, and encourage engagement. "
            f"{hashtag_rule}\nOutput ONLY the caption text."
        )
        content = [{"type": "text", "text": f"Write a caption for: {description}"}]
        return await self._message(system, content, max_tokens=300)


def encode_jpeg(image_bytes: bytes) -> str:
    """Base64-encode raw JPEG bytes for the vision API."""
    return base64.b64encode(image_bytes).decode("ascii")
