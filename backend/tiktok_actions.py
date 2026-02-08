"""High-level TikTok actions using PhoneFarm API."""
import asyncio
import re
import logging
from typing import Optional, Any
from dataclasses import dataclass

from api_client import PhoneFarmClient, ApiResponse
from ai_comments import AICommentGenerator, VideoContext
from config import Config

logger = logging.getLogger("tiktok_controller")


@dataclass
class DeviceInfo:
    """Device information - wrapper for compatibility."""
    device_id: str
    name: str
    model: str
    width: int
    height: int
    state: int  # 1 = online
    ip: str = ""

    @property
    def is_online(self) -> bool:
        return self.state == 1

    @classmethod
    def from_api(cls, device_id: str, data: dict) -> "DeviceInfo":
        return cls(
            device_id=device_id,
            name=data.get("name", ""),
            model=data.get("model", "Unknown"),
            width=data.get("width", 0),
            height=data.get("height", 0),
            state=data.get("state", 0),
            ip=data.get("ip", "")
        )

    @classmethod
    def from_device(cls, device: Any) -> "DeviceInfo":
        """Create from DeviceManager's Device object."""
        return cls(
            device_id=device.device_id,
            name=device.name,
            model=device.model,
            width=device.width,
            height=device.height,
            state=device.state,
            ip=device.ip
        )


class TikTokController:
    """High-level TikTok automation controller."""

    def __init__(self, config: Config, device_manager: Optional[Any] = None):
        self.config = config
        self._device_manager = device_manager
        # Pass device_manager to api client for WebSocket fallback
        self.api = PhoneFarmClient(config, device_manager)
        self.ai = AICommentGenerator(config)
        self._devices: dict[str, DeviceInfo] = {}  # Fallback cache

    async def __aenter__(self):
        await self.api.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.api.__aexit__(*args)

    def _get_coords(self, device: DeviceInfo, ui_key: str) -> tuple[int, int]:
        """Convert percentage-based UI coordinates to pixels."""
        ui = self.config.tiktok_ui.get(ui_key, {"x": 0.5, "y": 0.5})
        x = int(device.width * ui["x"])
        y = int(device.height * ui["y"])
        return x, y

    async def refresh_devices(self) -> list[DeviceInfo]:
        """Refresh and return list of devices."""
        logger.info("Refreshing device list...")

        # Prefer WebSocket-based device manager
        if self._device_manager:
            devices = await self._device_manager.refresh_devices()
            self._devices = {d.device_id: DeviceInfo.from_device(d) for d in devices}
            logger.info(f"Devices from WebSocket: {len(self._devices)}")
            return list(self._devices.values())

        # Fallback to HTTP API (may not work on iMouseXP)
        logger.warning("No device manager, falling back to HTTP API")
        response = await self.api.get_device_list()

        if not response.success:
            logger.error(f"Failed to get device list: {response.message}")
            return list(self._devices.values())

        if not response.data:
            logger.warning("Device list response has no data")
            return list(self._devices.values())

        # Handle different response formats
        self._devices = {}

        if isinstance(response.data, dict):
            for device_id, data in response.data.items():
                try:
                    self._devices[device_id] = DeviceInfo.from_api(device_id, data)
                    logger.info(f"Found device: {device_id} ({data.get('model', 'Unknown')})")
                except Exception as e:
                    logger.error(f"Error parsing device {device_id}: {e}")

        elif isinstance(response.data, list):
            for item in response.data:
                if isinstance(item, dict):
                    device_id = item.get("deviceid") or item.get("device_id") or item.get("id")
                    if device_id:
                        try:
                            self._devices[device_id] = DeviceInfo.from_api(device_id, item)
                            logger.info(f"Found device: {device_id}")
                        except Exception as e:
                            logger.error(f"Error parsing device: {e}")

        logger.info(f"Total devices found: {len(self._devices)}")
        return list(self._devices.values())

    async def get_devices(self) -> list[DeviceInfo]:
        """Get cached device list or refresh if empty."""
        # Check device manager first
        if self._device_manager and self._device_manager.devices:
            return [DeviceInfo.from_device(d) for d in self._device_manager.devices]

        if not self._devices:
            await self.refresh_devices()
        return list(self._devices.values())

    async def get_device(self, device_id: str) -> Optional[DeviceInfo]:
        """Get a specific device."""
        # Check device manager first
        if self._device_manager:
            device = self._device_manager.get_device(device_id)
            if device:
                return DeviceInfo.from_device(device)

        if device_id not in self._devices:
            await self.refresh_devices()
        return self._devices.get(device_id)

    # Screenshot and Analysis
    async def get_screenshot(self, device_id: str) -> Optional[str]:
        """Get screenshot as base64."""
        response = await self.api.get_screenshot(device_id)
        if response.success and response.data:
            return response.data.get("img")
        return None

    async def analyze_current_video(self, device_id: str) -> VideoContext:
        """Analyze the currently displayed TikTok video."""
        screenshot = await self.get_screenshot(device_id)
        context = VideoContext(screenshot_base64=screenshot)

        if screenshot and self.config.openai_api_key:
            try:
                analysis = await self.ai.analyze_screenshot(screenshot)
                context.caption = analysis.get("caption")
                context.hashtags = analysis.get("hashtags", [])
                context.username = analysis.get("username")
                context.ocr_text = analysis.get("content_description")
            except Exception as e:
                print(f"AI analysis failed: {e}")

        # Also try OCR for text extraction
        device = await self.get_device(device_id)
        if device:
            caption_area = self.config.tiktok_ui.get("caption_area", {})
            rect = [
                int(device.width * caption_area.get("x", 0)),
                int(device.height * caption_area.get("y", 0)),
                int(device.width * (caption_area.get("x", 0) + caption_area.get("w", 1))),
                int(device.height * (caption_area.get("y", 0) + caption_area.get("h", 1)))
            ]
            ocr_response = await self.api.ocr(device_id, rect)
            if ocr_response.success and ocr_response.data:
                texts = [item.get("text", "") for item in ocr_response.data if "text" in item]
                ocr_text = " ".join(texts)
                if ocr_text:
                    context.ocr_text = ocr_text
                    # Extract hashtags from OCR
                    hashtags = re.findall(r'#\w+', ocr_text)
                    context.hashtags = list(set(context.hashtags + hashtags))

        return context

    # Core Actions
    async def swipe_next(self, device_id: str) -> ApiResponse:
        """Swipe to next video."""
        return await self.api.swipe(
            device_id,
            direction="up",
            length=self.config.swipe_length
        )

    async def swipe_previous(self, device_id: str) -> ApiResponse:
        """Swipe to previous video."""
        return await self.api.swipe(
            device_id,
            direction="down",
            length=self.config.swipe_length
        )

    async def like_video(self, device_id: str, use_double_tap: bool = True) -> ApiResponse:
        """Like the current video."""
        device = await self.get_device(device_id)
        if not device:
            return ApiResponse(False, -1, None, "Device not found")

        if use_double_tap:
            # Double tap center of video
            x, y = self._get_coords(device, "video_center")
            await self.api.click(device_id, x, y, duration=self.config.click_duration)
            await asyncio.sleep(self.config.double_tap_interval / 1000)
            return await self.api.click(device_id, x, y, duration=self.config.click_duration)
        else:
            # Tap heart icon
            x, y = self._get_coords(device, "like_button")
            return await self.api.click(device_id, x, y, duration=self.config.click_duration)

    async def save_video(self, device_id: str) -> ApiResponse:
        """Save/bookmark the current video."""
        device = await self.get_device(device_id)
        if not device:
            return ApiResponse(False, -1, None, "Device not found")

        # Tap share button
        x, y = self._get_coords(device, "share_button")
        await self.api.click(device_id, x, y, duration=self.config.click_duration)

        # Wait for menu
        await asyncio.sleep(0.5)

        # Tap save button
        x, y = self._get_coords(device, "save_button")
        result = await self.api.click(device_id, x, y, duration=self.config.click_duration)

        # Tap elsewhere to close menu if still open
        await asyncio.sleep(0.3)
        await self.api.click(device_id, device.width // 2, device.height // 4)

        return result

    async def follow_creator(self, device_id: str) -> ApiResponse:
        """Follow the current video's creator."""
        device = await self.get_device(device_id)
        if not device:
            return ApiResponse(False, -1, None, "Device not found")

        x, y = self._get_coords(device, "follow_button")
        return await self.api.click(device_id, x, y, duration=self.config.click_duration)

    async def open_comments(self, device_id: str) -> ApiResponse:
        """Open comment section."""
        device = await self.get_device(device_id)
        if not device:
            return ApiResponse(False, -1, None, "Device not found")

        x, y = self._get_coords(device, "comment_button")
        return await self.api.click(device_id, x, y, duration=self.config.click_duration)

    async def type_comment(self, device_id: str, text: str) -> ApiResponse:
        """Type a comment in the comment input field."""
        device = await self.get_device(device_id)
        if not device:
            return ApiResponse(False, -1, None, "Device not found")

        # Tap comment input field
        x, y = self._get_coords(device, "comment_input")
        await self.api.click(device_id, x, y, duration=self.config.click_duration)
        await asyncio.sleep(0.3)

        # Type the comment character by character or use send_text for unicode
        if all(ord(c) < 128 for c in text):
            # ASCII text - can use send_key
            return await self.api.send_key(device_id, key=text)
        else:
            # Unicode text - use send_text
            return await self.api.send_text(device_id, text)

    async def post_comment(self, device_id: str) -> ApiResponse:
        """Submit the typed comment."""
        device = await self.get_device(device_id)
        if not device:
            return ApiResponse(False, -1, None, "Device not found")

        x, y = self._get_coords(device, "post_button")
        return await self.api.click(device_id, x, y, duration=self.config.click_duration)

    async def comment_on_video(
        self,
        device_id: str,
        comment: Optional[str] = None,
        use_ai: bool = True,
        ai_style: str = "friendly"
    ) -> dict:
        """Full comment flow: analyze, generate/use comment, post."""
        result = {"success": False, "comment": "", "message": ""}

        try:
            # Open comments
            await self.open_comments(device_id)
            await asyncio.sleep(0.5)

            # Generate comment if needed
            if comment is None and use_ai:
                context = await self.analyze_current_video(device_id)
                comment = await self.ai.generate_comment(context, style=ai_style)

            if not comment:
                result["message"] = "No comment provided and AI generation failed"
                return result

            result["comment"] = comment

            # Type comment
            await self.type_comment(device_id, comment)
            await asyncio.sleep(0.3)

            # Post comment
            response = await self.post_comment(device_id)
            result["success"] = response.success
            result["message"] = response.message

            # Close comments (tap outside)
            await asyncio.sleep(0.5)
            device = await self.get_device(device_id)
            if device:
                await self.api.click(device_id, device.width // 2, 50)

        except Exception as e:
            result["message"] = str(e)

        return result

    async def generate_comment_options(
        self,
        device_id: str,
        count: int = 3
    ) -> list[str]:
        """Generate multiple comment options for the current video."""
        context = await self.analyze_current_video(device_id)
        return await self.ai.generate_multiple_comments(context, count=count)

    # Batch Operations
    async def batch_action(
        self,
        device_ids: list[str],
        action: str,
        **kwargs
    ) -> dict[str, ApiResponse]:
        """Perform an action on multiple devices."""
        results = {}

        action_map = {
            "swipe_next": self.swipe_next,
            "swipe_previous": self.swipe_previous,
            "like": self.like_video,
            "save": self.save_video,
            "follow": self.follow_creator,
        }

        if action not in action_map:
            for device_id in device_ids:
                results[device_id] = ApiResponse(False, -1, None, f"Unknown action: {action}")
            return results

        action_fn = action_map[action]
        tasks = [action_fn(device_id, **kwargs) for device_id in device_ids]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for device_id, response in zip(device_ids, responses):
            if isinstance(response, Exception):
                results[device_id] = ApiResponse(False, -1, None, str(response))
            else:
                results[device_id] = response

        return results

    # Automation Sequences
    async def auto_browse(
        self,
        device_id: str,
        video_count: int = 10,
        like_probability: float = 0.3,
        comment_probability: float = 0.1,
        min_watch_time: float = 3.0,
        max_watch_time: float = 15.0
    ) -> dict:
        """Automated browsing with random engagement."""
        import random

        stats = {"videos": 0, "likes": 0, "comments": 0, "errors": []}

        for i in range(video_count):
            try:
                # Watch for random duration
                watch_time = random.uniform(min_watch_time, max_watch_time)
                await asyncio.sleep(watch_time)

                # Maybe like
                if random.random() < like_probability:
                    await self.like_video(device_id)
                    stats["likes"] += 1

                # Maybe comment
                if random.random() < comment_probability:
                    result = await self.comment_on_video(device_id, use_ai=True)
                    if result["success"]:
                        stats["comments"] += 1

                # Swipe to next
                await self.swipe_next(device_id)
                stats["videos"] += 1

            except Exception as e:
                stats["errors"].append(str(e))

        return stats
