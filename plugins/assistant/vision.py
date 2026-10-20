"""当前及明确引用图片输入；历史图片通过留档工具按需读取。"""

import asyncio
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit


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

    def __init__(self, config, archive=None):
        self.config = config
        self.archive = archive
        self.repetitions = {}

    @staticmethod
    def image_key(segment):
        # QQ 图片文件标识优先，临时下载参数不参与同图判断。
        file_id = segment.data.get("file")
        if isinstance(file_id, str):
            digest = re.search(r"[a-fA-F0-9]{32,64}", file_id)
            if digest:
                return digest.group().lower()
        url = platform_url(segment.data.get("url"))
        if not url:
            return file_id if isinstance(file_id, str) and file_id else None
        parsed = urlsplit(url)
        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if k.lower() not in {"rkey", "sign", "token", "timestamp", "expires"}
        ]
        return parsed._replace(query=urlencode(query), fragment="").geturl()

    def observe(self, bot, event):
        now = time.monotonic()
        self.repetitions = {
            key: {user: stamp for user, stamp in users.items() if now - stamp < 600}
            for key, users in self.repetitions.items()
            if any(now - stamp < 600 for stamp in users.values())
        }
        for origin, segment in image_segments(event):
            ident = self.image_key(segment)
            if origin != "当前消息" or ident is None or str(event.user_id) == str(bot.self_id):
                continue
            key = (int(bot.self_id), event.group_id, ident)
            self.repetitions.setdefault(key, {})[event.user_id] = now
        while len(self.repetitions) > 2000:
            oldest = min(self.repetitions, key=lambda key: max(self.repetitions[key].values()))
            del self.repetitions[oldest]

    async def prepare(self, bot, event):
        images = image_segments(event)
        content = []
        cached = {}
        if self.archive is not None and bot is not None:
            import json

            from .image_archive import ImageArchive

            scope = getattr(event, "group_id", -event.user_id)
            for origin, message in (
                ("当前消息", event),
                ("引用消息", getattr(event, "reply", None)),
            ):
                if message is None:
                    continue
                row = self.archive.db.execute(
                    "SELECT metadata FROM messages WHERE bot=? AND grp=? AND message_id=?",
                    (int(bot.self_id), scope, message.message_id),
                ).fetchone()
                refs = json.loads(row[0] or "{}").get("images", []) if row else []
                for ref in refs:
                    visible = {k: v for k, v in ref.items() if k != "source_url"}
                    content.append(
                        {
                            "type": "text",
                            "text": origin + "图片留档：" + json.dumps(visible, ensure_ascii=False),
                        }
                    )
                    if ref.get("id"):
                        _, parts = ImageArchive(self.archive).read(
                            ref["id"], int(bot.self_id), {scope}
                        )
                        if parts:
                            cached[(origin, ref["index"])] = parts[-1]["image_url"]["url"]
        positions = {}
        failed = max(0, len(images) - self.config.vision_max_images)
        for index, (origin, seg) in enumerate(images[: self.config.vision_max_images], 1):
            position = positions.get(origin, 0)
            positions[origin] = position + 1
            url = cached.get((origin, position)) or platform_url(seg.data.get("url"))
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
            source_id = (
                getattr(getattr(event, "reply", None), "message_id", None)
                if origin == "引用消息"
                else event.message_id
            )
            key = (
                int(getattr(bot, "self_id", 0)),
                getattr(event, "group_id", 0),
                self.image_key(seg),
            )
            users = self.repetitions.get(key, {})
            repeated = sum(time.monotonic() - stamp < 600 for stamp in users.values()) >= 2
            content.extend(
                [
                    {
                        "type": "text",
                        "text": f"图片{index}（{origin}，消息ID={source_id}）"
                        + ("【本群10分钟内至少两位群友复读，优先考虑收藏】" if repeated else "")
                        + "：",
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


def limit_images(parts, maximum):
    """合并连续消息时共用图片上限，保留每张图片的来源标签。"""
    result = []
    count = 0
    skipped = 0
    for index, part in enumerate(parts):
        if part.get("type") == "image_url":
            count += 1
            if count > maximum:
                skipped += 1
                continue
        elif (
            index + 1 < len(parts)
            and parts[index + 1].get("type") == "image_url"
            and count >= maximum
        ):
            continue
        result.append(part)
    if skipped:
        result.append(
            {"type": "text", "text": f"[{skipped}张连续消息图片超过总上限，未读取，不能猜测内容。]"}
        )
    return result
