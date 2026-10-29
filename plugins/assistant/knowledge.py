"""认知字段与平台显示资料。"""

import json

GLOBAL_FIELDS = ("总体认知",)
GROUP_FIELDS = ("角色", "互动习惯", "互动关系", "补充认知")
SCENE_FIELDS = ("会话印象", *GROUP_FIELDS)
KNOWLEDGE_LIMIT = 12000


def read_directory(root):
    path = root / "data/contacts.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def display_name(db, bot, user, directory=None):
    """仅使用平台昵称，不从认知中的称呼推断当前昵称。"""
    name = (directory or {}).get(str(bot), {}).get("users", {}).get(str(user), {}).get("name")
    if name:
        return name
    for row in db.execute(
        "SELECT metadata FROM messages WHERE bot=? AND usr=? AND metadata IS NOT NULL "
        "ORDER BY created DESC,id DESC",
        (bot, user),
    ):
        data = json.loads(row[0] or "{}")
        name = data.get("sender_nickname")
        if isinstance(name, str) and name.strip() and name not in (str(user), f"QQ用户{user}"):
            return name.strip()
    return "昵称待同步"


def scope_name(db, bot, group, directory=None):
    if group < 0:
        return "私聊"
    name = (directory or {}).get(str(bot), {}).get("groups", {}).get(str(group), {}).get("name")
    if name:
        return name
    for row in db.execute(
        "SELECT metadata FROM messages WHERE bot=? AND grp=? AND metadata IS NOT NULL "
        "ORDER BY created DESC,id DESC",
        (bot, group),
    ):
        name = json.loads(row[0] or "{}").get("group_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return f"群名待同步（{group}）"
