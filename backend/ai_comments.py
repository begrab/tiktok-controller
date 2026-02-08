"""AI-powered comment generation using OpenAI."""
import base64
from typing import Optional
from dataclasses import dataclass

from openai import AsyncOpenAI

from config import Config


@dataclass
class VideoContext:
    """Context extracted from a TikTok video."""
    caption: Optional[str] = None
    hashtags: list[str] = None
    username: Optional[str] = None
    ocr_text: Optional[str] = None
    screenshot_base64: Optional[str] = None

    def __post_init__(self):
        if self.hashtags is None:
            self.hashtags = []


class AICommentGenerator:
    """Generate context-aware comments using OpenAI."""

    def __init__(self, config: Config):
        self.config = config
        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self.config.openai_api_key:
                raise ValueError("OpenAI API key not configured")
            self._client = AsyncOpenAI(api_key=self.config.openai_api_key)
        return self._client

    def set_api_key(self, api_key: str):
        """Update the OpenAI API key."""
        self.config.openai_api_key = api_key
        self._client = None  # Reset client to use new key

    async def analyze_screenshot(self, screenshot_base64: str) -> dict:
        """Analyze a screenshot to extract video context."""
        response = await self.client.chat.completions.create(
            model=self.config.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": """You are analyzing a TikTok video screenshot. Extract:
1. Any visible caption/description text
2. Hashtags (starting with #)
3. Username of the creator (usually starts with @)
4. What the video appears to be about (content description)
5. The mood/vibe of the content

Return as JSON with keys: caption, hashtags (array), username, content_description, mood"""
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{screenshot_base64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": "Analyze this TikTok screenshot and extract the context."
                        }
                    ]
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=500
        )

        import json
        return json.loads(response.choices[0].message.content)

    async def generate_comment(
        self,
        context: VideoContext,
        style: str = "friendly",
        length: str = "short",
        include_emoji: bool = True,
        custom_instructions: Optional[str] = None
    ) -> str:
        """Generate a contextually appropriate comment."""

        style_prompts = {
            "friendly": "warm, supportive, and genuinely engaged",
            "funny": "witty, humorous, with clever observations",
            "supportive": "encouraging, positive, and uplifting",
            "curious": "asking thoughtful questions, showing genuine interest",
            "impressed": "expressing admiration and appreciation",
            "relatable": "sharing similar experiences or feelings",
        }

        length_guides = {
            "short": "Keep it to 1-2 sentences, very brief",
            "medium": "2-3 sentences with a bit more detail",
            "long": "3-5 sentences with full engagement"
        }

        context_parts = []
        if context.caption:
            context_parts.append(f"Caption: {context.caption}")
        if context.hashtags:
            context_parts.append(f"Hashtags: {', '.join(context.hashtags)}")
        if context.username:
            context_parts.append(f"Creator: {context.username}")
        if context.ocr_text:
            context_parts.append(f"Text on screen: {context.ocr_text}")

        context_str = "\n".join(context_parts) if context_parts else "No text context available"

        messages = [
            {
                "role": "system",
                "content": f"""You are helping write authentic TikTok comments.

Style: {style_prompts.get(style, style_prompts['friendly'])}
Length: {length_guides.get(length, length_guides['short'])}
{"Include 1-2 relevant emojis" if include_emoji else "Do not use emojis"}

Write comments that:
- Feel genuine and human, not bot-like
- Are relevant to the specific content
- Match the energy/mood of the video
- Avoid generic phrases like "Great video!" or "Love this!"
- Don't be overly promotional or spammy

{f"Additional instructions: {custom_instructions}" if custom_instructions else ""}

Output ONLY the comment text, nothing else."""
            },
            {
                "role": "user",
                "content": f"Write a comment for this TikTok:\n\n{context_str}"
            }
        ]

        # If we have a screenshot, include it for visual context
        if context.screenshot_base64:
            messages[1] = {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{context.screenshot_base64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": f"Write a comment for this TikTok:\n\n{context_str}"
                    }
                ]
            }

        response = await self.client.chat.completions.create(
            model=self.config.openai_model,
            messages=messages,
            max_tokens=200,
            temperature=0.8  # Slightly higher for more varied comments
        )

        return response.choices[0].message.content.strip()

    async def generate_multiple_comments(
        self,
        context: VideoContext,
        count: int = 3,
        styles: Optional[list[str]] = None
    ) -> list[str]:
        """Generate multiple comment options."""
        if styles is None:
            styles = ["friendly", "funny", "supportive"]

        comments = []
        for i in range(min(count, len(styles))):
            comment = await self.generate_comment(
                context,
                style=styles[i % len(styles)]
            )
            comments.append(comment)

        return comments

    async def generate_post_caption(
        self,
        description: str,
        mood: str = "engaging",
        include_hashtags: bool = True,
        hashtag_count: int = 5
    ) -> str:
        """Generate a caption for posting content."""
        response = await self.client.chat.completions.create(
            model=self.config.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a TikTok content creator writing captions.

Create a {mood} caption that:
- Is attention-grabbing and authentic
- Encourages engagement (comments, shares)
- Feels natural, not forced
{"- Include " + str(hashtag_count) + " relevant trending hashtags at the end" if include_hashtags else "- Do not include hashtags"}

Output ONLY the caption text."""
                },
                {
                    "role": "user",
                    "content": f"Write a caption for: {description}"
                }
            ],
            max_tokens=300,
            temperature=0.7
        )

        return response.choices[0].message.content.strip()
