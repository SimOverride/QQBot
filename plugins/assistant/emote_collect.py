"""可恢复的图片候选；查看与保存审核分离，不接受模型自造路径。"""

import base64
import hashlib
import json

from .http import ServiceError
from .image_download import download_image
from .prompt_store import tool_definition

COLLECT_FUNCTIONS = [
    tool_definition("emote_candidates"),
    tool_definition("search_emote_images"),
    tool_definition("inspect_emote_image"),
    tool_definition("save_emote"),
]

IMAGE_FUNCTIONS = [
    tool_definition("search_images"),
    tool_definition("inspect_image"),
    tool_definition("choose_image"),
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
    def __init__(
        self,
        library,
        review,
        downloader=download_image,
        max_bytes=5 * 1024 * 1024,
        cache_path=None,
        bot=0,
    ):
        self.library, self.review, self.downloader = library, review, downloader
        self.bot = bot
        self.max_bytes = library.max_bytes if library is not None else max_bytes
        self.candidates, self.inspected = {}, {}
        self.cache_path = cache_path
        if cache_path and cache_path.exists():
            self.candidates = json.loads(cache_path.read_text(encoding="utf-8"))
        self.saved = 0
        self.attempts = 0
        self.download_attempts = 0

    def add(self, url, source, hint=""):
        for ident, item in self.candidates.items():
            if item["url"] == url:
                return ident
        ident = "candidate-" + hashlib.sha256(url.encode()).hexdigest()[:24]
        self.candidates[ident] = {"url": url, "source": source, "hint": hint[:200]}
        if self.cache_path and url.startswith(("http://", "https://")):
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {k: v for k, v in self.candidates.items() if not v["url"].startswith("data:")},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.cache_path)
        return ident

    def listing(self, offset=0):
        entries = list(self.candidates.items())
        return {
            "status": "ok",
            "candidates": [
                {"id": ident, "source": item["source"], "hint": item["hint"]}
                for ident, item in entries[offset : offset + 30]
            ],
            "next_offset": offset + 30 if offset + 30 < len(entries) else None,
        }

    async def inspect(self, ident):
        if ident not in self.candidates:
            return {"status": "unknown_candidate"}, []
        if ident in self.inspected:
            item = self.inspected[ident]
            return {
                "status": "inspected",
                "eligible": item["eligible"],
                "description": item["description"],
            }, []
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
        self.inspected[ident] = {
            "data": data,
            "digest": digest,
            "suffix": suffix,
            "eligible": None,
            "description": "",
            "preview": preview,
        }
        return {"status": "inspected", "id": ident}, [
            {"type": "text", "text": "候选原图，id=" + ident},
            preview,
        ]

    async def ensure_review(self, ident):
        item = self.inspected.get(ident)
        if item is None:
            return False
        if item["eligible"] is None:
            verdict = await self.review(item["preview"])
            item["eligible"] = isinstance(verdict, dict) and verdict.get("eligible") is True
            item["description"] = (
                verdict.get("description", "") if isinstance(verdict, dict) else ""
            )
        return item["eligible"] is True

    def choose(self, ident):
        item = self.inspected.get(ident)
        if not self.candidates.get(ident, {}).get("source", "").startswith("web:"):
            return None
        return item if item and item["eligible"] else None

    def save(self, ident, description):
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1200:
            return {"status": "invalid_description"}
        if self.library is None:
            return {"status": "library_unavailable"}
        item = self.inspected.get(ident)
        if not item or not item["eligible"]:
            return {"status": "not_reviewed_or_ineligible"}
        if "saved_id" in item:
            return {"status": "already_saved", "id": item["saved_id"]}
        result = self.library.import_image(
            item["data"],
            item["suffix"],
            description.strip(),
            self.candidates[ident]["source"],
            self.bot,
        )
        if result.get("status") in ("saved", "already_saved"):
            item["saved_id"] = result["id"]
            self.saved += 1
        return result
