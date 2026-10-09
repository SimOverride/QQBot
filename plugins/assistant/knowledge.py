"""个人总体认知、会话印象及平台显示资料。"""

import json

GLOBAL_FIELDS = ("总体认知",)
GROUP_FIELDS = ("角色", "互动习惯", "互动关系", "补充认知")
SCENE_FIELDS = ("会话印象", *GROUP_FIELDS)
KNOWLEDGE_LIMIT = 12000


def prose(rows):
    """合并已有记录并保留旧分类文字，不擅自丢弃维护者内容。"""
    lines = []
    for row in rows:
        value = row["value"].strip()
        field = row["field"]
        if not value:
            continue
        line = (
            value
            if field.split(":", 1)[0] in (*GLOBAL_FIELDS, "会话印象")
            else f"{field.split(':', 1)[0]}：{value}"
        )
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def group_value(rows):
    """保留原群内分类；未分类段落完整归入补充认知，不推断类别。"""
    result = {field: [] for field in GROUP_FIELDS}
    for row in rows:
        field = row["field"].split(":", 1)[0]
        value = row["value"].strip()
        target = field if field in GROUP_FIELDS else "补充认知"
        if field not in (*GROUP_FIELDS, "会话印象"):
            value = f"{field}：{value}" if value else ""
        if value and value not in result[target]:
            result[target].append(value)
    return {field: "\n".join(values) for field, values in result.items()}


def person_value(db, bot, user):
    rows = db.execute(
        "SELECT * FROM facts WHERE bot=? AND usr=? ORDER BY grp,updated,field", (bot, user)
    ).fetchall()
    scopes = {
        r[0]
        for r in db.execute("SELECT DISTINCT grp FROM messages WHERE bot=? AND usr=?", (bot, user))
        if r[0] > 0 or r[0] == -user
    }
    scopes.update(r["grp"] for r in rows if r["grp"] > 0 or r["grp"] == -user)
    value = {
        "总体认知": prose([r for r in rows if r["grp"] == 0]),
        "会话印象": {
            str(g): (group_value if g > 0 else prose)([r for r in rows if r["grp"] == g])
            for g in sorted(scopes)
        },
    }
    return value, [dict(r) for r in rows]


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
