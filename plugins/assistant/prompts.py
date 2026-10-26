import json
from datetime import datetime, timedelta, timezone

from .personality import DEFAULTS
from .prompt_store import prompt_text, tool_definition


def system_prompt(search_enabled: bool, personality: dict[str, str] | None = None) -> str:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    return (
        prompt_text("prompts.system_prompt.0", {"today": today})
        + (prompt_text("search.enabled") if search_enabled else prompt_text("search.disabled"))
        + prompt_text("prompts.system_prompt.1")
        + json.dumps(personality if personality is not None else DEFAULTS, ensure_ascii=False)
        + prompt_text("chat.expression")
    )


SEARCH_FUNCTION = tool_definition("web_search")
