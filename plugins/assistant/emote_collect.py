"""Per-turn candidate registry; no arbitrary paths or model-invented URLs."""

import base64
import hashlib

from .emotes import tool
from .http import ServiceError
from .image_download import download_image

COLLECT_FUNCTIONS = [
    tool(
        "emote_candidates",
        "查看本轮群聊图片候选。可自主收藏通用表情，不收集私人照片、截图隐私或敏感资料。",
        {},
    ),
    tool(
        "search_emote_images",
        "按当前语境自主联网找合适的表情包。先用本地图库，缺合适的再搜；不把群聊隐私放进查询。",
        {"query": {"type": "string"}},
    ),
    tool(
        "inspect_emote_image",
        "下载并查看候选原图，审核是否适合通用表情收藏；只接受候选id。",
        {"id": {"type": "string"}},
    ),
    tool(
        "save_emote",
        "收藏已查看且审核通过的候选，自动去重并保存图片内容记忆。每轮最多2张。保存不代表已发送。",
        {"id": {"type": "string"}},
    ),
]


def image_data(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ServiceError("候选不是支持的图片文件。")


class EmoteCollector:
    def __init__(self, library, review, downloader=download_image):
        self.library, self.review, self.downloader = library, review, downloader
        self.candidates, self.inspected = {}, {}
        self.saved = 0
        self.attempts = 0

    def add(self, url, source, hint=""):
        if len(self.candidates) >= 15:
            return None
        for ident, item in self.candidates.items():
            if item["url"] == url:
                return ident
        ident = f"image-{len(self.candidates) + 1}"
        self.candidates[ident] = {"url": url, "source": source, "hint": hint[:200]}
        return ident

    def listing(self):
        return {
            "status": "ok",
            "candidates": [
                {"id": ident, "source": item["source"], "hint": item["hint"]}
                for ident, item in self.candidates.items()
            ],
        }

    async def inspect(self, ident):
        if ident not in self.candidates:
            return {"status": "unknown_candidate"}, []
        if ident in self.inspected:
            item = self.inspected[ident]
            return {
                "status": "reviewed",
                "eligible": item["eligible"],
                "description": item["description"],
            }, []
        if self.attempts >= 2:
            return {"status": "inspection_limit"}, []
        self.attempts += 1
        data = await self.downloader(self.candidates[ident]["url"], self.library.max_bytes)
        if not data or len(data) > self.library.max_bytes:
            raise ServiceError("候选图片为空或超过大小上限。")
        suffix, mime = image_data(data)
        digest = hashlib.sha256(data).hexdigest()
        preview = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")},
        }
        remembered = self.library.recall(digest)
        verdict = (
            {"eligible": True, "description": remembered}
            if remembered
            else await self.review(preview)
        )
        eligible = isinstance(verdict, dict) and verdict.get("eligible") is True
        description = verdict.get("description", "") if isinstance(verdict, dict) else ""
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1200:
            eligible, description = False, "审核未通过或无法确定图像内容"
        self.inspected[ident] = {
            "data": data if eligible else b"",
            "digest": digest,
            "suffix": suffix,
            "eligible": eligible,
            "description": description,
        }
        return {"status": "reviewed", "eligible": eligible, "description": description}, (
            []
            if remembered
            else [
                {"type": "text", "text": "收藏候选原图，id=" + ident},
                preview,
            ]
        )

    def save(self, ident):
        item = self.inspected.get(ident)
        if not item or not item["eligible"]:
            return {"status": "not_reviewed_or_ineligible"}
        if "saved_id" in item:
            return {"status": "already_saved", "id": item["saved_id"]}
        if self.saved >= 2:
            return {"status": "save_limit"}
        result = self.library.import_image(
            item["data"], item["suffix"], item["description"], self.candidates[ident]["source"]
        )
        if result.get("status") in ("saved", "already_saved"):
            item["saved_id"] = result["id"]
            self.saved += 1
        return result
