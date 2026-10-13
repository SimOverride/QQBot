"""Authority comes from local owner IDs and verified platform roles, never display names."""

import json

from .config import Config
from .prompt_store import prompt_text


def permission_level(config: Config, user_id: int, role: str | None = None) -> int:
    if user_id in config.bot_owners:
        return 2
    return 1 if role in ("owner", "admin") else 0


def identity_context(
    config: Config,
    bot_id: int,
    group_id: int,
    user_id: int,
    names: dict[int, str] | None = None,
    group_role: str | None = None,
    root=None,
) -> str:
    from . import prompt_store
    from .names import Names

    resolved = Names(root or prompt_store.ROOT, bot_id, {**config.user_names, **(names or {})})
    names = resolved.names
    group_role = group_role if group_id > 0 else None

    def person(identifier: int) -> dict:
        return {
            "qq": identifier,
            "name": names.get(identifier, f"QQ用户{identifier}"),
        }

    data = {
        "bot_qq": bot_id,
        "bot_self": {**person(bot_id), "is_self": True, "说明": "这是你自己的账号"},
        "group_id": group_id if group_id > 0 else None,
        "conversation": "群聊" if group_id > 0 else "私聊",
        "bot_owners": [person(uid) for uid in sorted(config.bot_owners)],
        "qq_group_owner": (
            person(user_id) if group_role == "owner" else "未提供，不能从机器人所有者名单推断"
        ),
        "current_speaker": {
            **person(user_id),
            "group_role": group_role,
            "role": (
                "机器人所有者"
                if permission_level(config, user_id) == 2
                else {"owner": "群主", "admin": "群管理员"}.get(group_role, "普通用户")
            ),
        },
    }
    return prompt_text("identity.identity_context.0") + json.dumps(data, ensure_ascii=False)
