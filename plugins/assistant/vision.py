"""Ephemeral native image inputs for the active chat model."""

import asyncio
import re
from urllib.parse import urlsplit


def image_segments(event):
    result = [
        ("当前消息", seg) for seg in getattr(event, "original_message", []) if seg.type == "image"
    ]
    reply = getattr(event, "reply", None)
    result.extend(
        ("引用消息", seg) for seg in (getattr(reply, "message", None) or []) if seg.type == "image"
    )
    return result


def platform_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            return None
        if any(
            host == domain or host.endswith("." + domain)
            for domain in ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.cn")
        ):
            return value
    except ValueError:
        pass
    return None


class Vision:
    """Resolve platform image references without calling a separate model."""

    def __init__(self, config):
        self.config = config

    async def prepare(self, bot, event):
        images = image_segments(event)
        content = []
        failed = max(0, len(images) - self.config.vision_max_images)
        for index, (origin, seg) in enumerate(images[: self.config.vision_max_images], 1):
            url = platform_url(seg.data.get("url"))
            file_id = seg.data.get("file", "")
            if not url and isinstance(file_id, str) and re.fullmatch(r"[\w.{}-]{1,256}", file_id):
                try:
                    info = await asyncio.wait_for(bot.get_image(file=file_id), 8)
                    url = platform_url(info.get("url")) if isinstance(info, dict) else None
                except Exception:
                    pass
            if not url:
                failed += 1
                continue
            content.extend(
                [
                    {
                        "type": "text",
                        "text": f"图片{index}（{origin}，消息ID={event.message_id}）：",
                    },
                    {"type": "image_url", "image_url": {"url": url}},
                ]
            )
        if failed:
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"[{failed}张图片未附上：取图失败或超过数量上限，不能猜测未附图片内容。]"
                    ),
                }
            )
        return content


def multimodal_content(text, images, provider):
    """Convert ephemeral parts to the active provider's native user input shape."""
    if not images:
        return text
    parts = [{"type": "text", "text": text}, *images]
    if provider != "openai":
        return parts
    return [
        {"type": "input_text", "text": p["text"]}
        if p["type"] == "text"
        else {"type": "input_image", "image_url": p["image_url"]["url"]}
        for p in parts
    ]
