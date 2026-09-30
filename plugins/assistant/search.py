from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx

from .config import Config
from .http import ServiceError, post_json


@dataclass
class SearchResult:
    title: str
    url: str
    content: str

    def numbered(self, number: int) -> dict:
        return {"id": number, **asdict(self)}


class Search:
    def __init__(self, client: httpx.AsyncClient, config: Config):
        self.client, self.config = client, config

    @property
    def enabled(self) -> bool:
        return bool(self.config.search_api_key.get_secret_value().strip())

    async def run(self, query: str, request_id: str) -> list[SearchResult]:
        if not self.enabled:
            raise ServiceError("搜索未配置，当前无法联网。")
        data = await post_json(
            self.client,
            "https://api.tavily.com/search",
            self.config.search_api_key.get_secret_value(),
            {
                "query": query,
                "max_results": self.config.search_max_results,
                "search_depth": "basic",
                "include_raw_content": False,
                "include_answer": False,
            },
            self.config.search_timeout_seconds,
            "搜索",
            request_id,
        )
        if not isinstance(data.get("results"), list):
            raise ServiceError("搜索返回了无效数据。")
        results = []
        for row in data["results"]:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if not isinstance(url, str) or len(url) > 600 or any(c.isspace() for c in url):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    continue
                if parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            title, content = row.get("title", ""), row.get("content", "")
            if not isinstance(title, str) or not isinstance(content, str):
                continue
            results.append(SearchResult(" ".join(title.split())[:100], url, content[:1500]))
            if len(results) >= self.config.search_max_results:
                break
        return results

    async def images(self, query, request_id):
        if not self.enabled:
            raise ServiceError("图片搜索未配置。")
        data = await post_json(
            self.client,
            "https://api.tavily.com/search",
            self.config.search_api_key.get_secret_value(),
            {
                "query": query,
                "max_results": 5,
                "search_depth": "basic",
                "include_images": True,
                "include_image_descriptions": True,
                "include_raw_content": False,
                "include_answer": False,
            },
            self.config.search_timeout_seconds,
            "图片搜索",
            request_id,
        )
        rows = data.get("images", [])
        if not isinstance(rows, list):
            raise ServiceError("图片搜索响应无效。")
        images = []
        for row in rows[:10]:
            url = row if isinstance(row, str) else row.get("url") if isinstance(row, dict) else None
            if not isinstance(url, str) or len(url) > 8192:
                continue
            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    continue
            except ValueError:
                continue
            hint = row.get("description", "") if isinstance(row, dict) else ""
            images.append({"url": url, "description": hint[:200] if isinstance(hint, str) else ""})
            if len(images) == 5:
                break
        return images
