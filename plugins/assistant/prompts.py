import json
from datetime import datetime, timedelta, timezone

from .personality import DEFAULTS
from .prompt_store import prompt_text


def system_prompt(search_enabled: bool, personality: dict[str, str] | None = None) -> str:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    return (
        prompt_text("prompts.system_prompt.0", {"today": today})
        + (prompt_text("search.enabled") if search_enabled else prompt_text("search.disabled"))
        + prompt_text("prompts.system_prompt.1")
        + json.dumps(personality if personality is not None else DEFAULTS, ensure_ascii=False)
    )


SEARCH_FUNCTION = {
    "name": "web_search",
    "description": "查询实时信息或核查事实。结合上下文构造独立查询，只含必要关键词。",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "1至500字的独立查询"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
