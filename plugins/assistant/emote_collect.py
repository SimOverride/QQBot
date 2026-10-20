"""Per-turn candidate registry; no arbitrary paths or model-invented URLs."""

import base64
import hashlib

from .emotes import tool
from .http import ServiceError
from .image_download import download_image

COLLECT_FUNCTIONS = [
    tool(
        "emote_candidates",
        "查看本轮群聊图片候选，由当前人设与聊天语境判断收藏偏好，优先考虑群内复读和人设相关表情。",
        {},
    ),
    tool(
        "search_emote_images",
        "按当前语境自主联网找合适的表情包。先用本地图库，缺合适的再搜；不把群聊隐私放进查询。",
        {"query": {"type": "string"}},
    ),
    tool(
        "inspect_emote_image",
        "下载并查看候选原图，只审核敏感内容和隐私等限制；是否收藏由当前聊天模型决定。只接受候选id。",
        {"id": {"type": "string"}},
    ),
    tool(
        "save_emote",
        "收藏已查看且审核通过的候选，自动去重并保存图片内容记忆。每轮最多2张。保存不代表已发送。",
        {"id": {"type": "string"}},
    ),
]

IMAGE_FUNCTIONS = [
    tool(
        "search_images",
        "联网搜索辅助解释的图片，不要求是表情包；查询不得包含聊天隐私。",
        {"query": {"type": "string"}},
    ),
    tool(
        "inspect_image",
        "查看搜索候选原图并做安全审核，只接受本轮候选id。",
        {"id": {"type": "string"}},
    ),
    tool(
        "choose_image",
        "选择已查看且安全审核通过的搜索图片随本次回复发送，不收藏入库。"
        "每次回复与本地表情合计最多一张，后选覆盖先选。",
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
    def __init__(self, library, review, downloader=download_image, max_bytes=5 * 1024 * 1024):
        self.library, self.review, self.downloader = library, review, downloader
        self.max_bytes = library.max_bytes if library is not None else max_bytes
        self.candidates, self.inspected = {}, {}
        self.saved = 0
        self.attempts = 0
        self.download_attempts = 0

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
        if self.download_attempts >= 4:
            return {"status": "download_limit"}, []
        self.download_attempts += 1
        try:
            url = self.candidates[ident]["url"]
            if url.startswith("data:image/"):
                try:
                    if len(url) > self.max_bytes * 4 // 3 + 256:
                        raise ValueError
                    data = base64.b64decode(url.split(",", 1)[1], validate=True)
                except (ValueError, IndexError) as error:
                    raise ServiceError("留档图片编码无效。") from error
            else:
                data = await self.downloader(url, self.max_bytes)
            if not data or len(data) > self.max_bytes:
                raise ServiceError("候选图片为空或超过大小上限。")
            suffix, mime = image_data(data)
        except ServiceError as exc:
            return {"status": "download_failed", "hint": str(exc)}, []
        self.attempts += 1
        digest = hashlib.sha256(data).hexdigest()
        preview = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")},
        }
        # 内容描述不是安全审核记录，不能因为曾经记忆过就跳过审核。
        verdict = await self.review(preview)
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
        return {"status": "reviewed", "eligible": eligible, "description": description}, [
            {"type": "text", "text": "候选原图，安全审核不代表收藏偏好，id=" + ident},
            preview,
        ]

    def choose(self, ident):
        item = self.inspected.get(ident)
        if not self.candidates.get(ident, {}).get("source", "").startswith("web:"):
            return None
        return item if item and item["eligible"] else None

    def save(self, ident):
        if self.library is None:
            return {"status": "library_unavailable"}
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
