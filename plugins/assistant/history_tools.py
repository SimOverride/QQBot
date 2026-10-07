"""Read-only, request-scoped access to archived conversations."""

from datetime import datetime, timedelta, timezone

from .prompt_store import prompt_text

TZ = timezone(timedelta(hours=8))
HISTORY_FUNCTION = {
    "name": "read_history",
    "description": (
        "主动查询本地历史和记忆。先list查会话；search按时间/发送者/关键词查消息；"
        "around读前后文；facts查当前用户认知。空关键词可查全部发言。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "search", "around", "facts"]},
            "scope": {"type": ["integer", "null"], "description": "会话ID；null搜索全部获准会话"},
            "sender": {"type": ["integer", "null"], "description": "发送者QQ，查自己用bot_qq"},
            "query": {"type": "string", "description": "原文包含的关键词；空字符串不过滤"},
            "start": {
                "type": ["string", "null"],
                "description": "含时区ISO时间或YYYY-MM-DD，包含起点",
            },
            "end": {"type": ["string", "null"], "description": "时间上界，不包含；日期按UTC+8"},
            "anchor": {"type": ["integer", "null"], "description": "around使用搜索结果的id"},
            "offset": {"type": "integer", "description": "分页偏移，从0开始"},
        },
        "required": ["action", "scope", "sender", "query", "start", "end", "anchor", "offset"],
        "additionalProperties": False,
    },
}


class HistoryTools:
    def __init__(self, archive, key, groups=(), send_callback=None, group_names=None):
        self.archive, self.key = archive, key
        self.send_callback = send_callback
        self.group_names = group_names or {}
        self.send_attempted = False
        self.emotes = None
        self.collector = None
        self.selected_emote = None
        self.offered_emotes = set()
        self.emote_hashes = {}
        self.scopes = {key[1]}
        if key[1] == -key[2]:
            self.scopes.update(g for g in groups if type(g) is int and g > 0)

    def instructions(self):
        return prompt_text(
            "history_tools.instructions.0",
            {"now": datetime.now(TZ).isoformat(), "bot_qq": self.key[0], "scope": self.key[1]},
        )

    async def send(self, args):
        if (
            not isinstance(args, dict)
            or set(args) != {"group_id", "text"}
            or type(args["group_id"]) is not int
            or not isinstance(args["text"], str)
            or not 1 <= len(args["text"].strip()) <= 500
        ):
            return {"status": "invalid_arguments"}
        if not self.send_callback or args["group_id"] <= 0 or args["group_id"] not in self.scopes:
            return {"status": "access_denied"}
        if self.send_attempted:
            return {"status": "already_attempted", "hint": "本次请求不能再次发送"}
        self.send_attempted = True
        return await self.send_callback(args["group_id"], args["text"].strip())

    @staticmethod
    def timestamp(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError
        dt = datetime.fromisoformat(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp()

    def run(self, args):
        try:
            if not isinstance(args, dict) or set(args) != set(
                HISTORY_FUNCTION["parameters"]["required"]
            ):
                raise ValueError
            action, scope = args["action"], args["scope"]
            if action not in ("list", "search", "around", "facts"):
                raise ValueError
            if scope is not None and (type(scope) is not int or scope not in self.scopes):
                return {"status": "access_denied"}
            if type(args["offset"]) is not int or not 0 <= args["offset"] <= 1000000:
                raise ValueError
            if not isinstance(args["query"], str) or len(args["query"]) > 200:
                raise ValueError
            for field in ("sender", "anchor"):
                if args[field] is not None and type(args[field]) is not int:
                    raise ValueError
            start, end = self.timestamp(args["start"]), self.timestamp(args["end"])
            if start is not None and end is not None and start >= end:
                raise ValueError
            scopes = sorted(self.scopes if scope is None else {scope})
            where = "bot=? AND grp IN (" + ",".join("?" for _ in scopes) + ")"
            params = [self.key[0], *scopes]
            db = self.archive.db
            if action == "list":
                rows = db.execute(
                    "SELECT grp,MIN(created) AS first_record,MAX(created) AS last_record,"
                    f"COUNT(*) AS count FROM messages WHERE {where} GROUP BY grp",
                    params,
                ).fetchall()
                return {
                    "status": "ok",
                    "current_scope": self.key[1],
                    "allowed_scopes": scopes,
                    "group_names": self.group_names,
                    "coverage": [dict(r) for r in rows],
                    "coverage_note": "仅为现存记录范围，不保证期间完整",
                }
            if action == "facts":
                return {
                    "status": "ok",
                    "facts": self.archive.facts(self.key),
                    "subject_qq": self.key[2],
                    "shared": self.archive.shared_knowledge(self.key)[:20],
                }
            if action == "around":
                anchor = db.execute(
                    f"SELECT * FROM messages WHERE {where} AND id=?", [*params, args["anchor"]]
                ).fetchone()
                if anchor is None:
                    return {"status": "not_found"}
                base = [
                    self.key[0],
                    anchor["grp"],
                    anchor["created"],
                    anchor["created"],
                    anchor["id"],
                ]
                before = db.execute(
                    "SELECT * FROM messages WHERE bot=? AND grp=? AND "
                    "(created<? OR (created=? AND id<=?)) ORDER BY created DESC,id DESC LIMIT 6",
                    base,
                ).fetchall()
                after = db.execute(
                    "SELECT * FROM messages WHERE bot=? AND grp=? AND "
                    "(created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT 5",
                    base,
                ).fetchall()
                rows = list(reversed(before)) + list(after)
                more = False
            else:
                for column, op, value in (
                    ("usr", "=", args["sender"]),
                    ("created", ">=", start),
                    ("created", "<", end),
                ):
                    if value is not None:
                        where += f" AND {column}{op}?"
                        params.append(value)
                if args["query"]:
                    where += " AND instr(lower(content),lower(?))>0"
                    params.append(args["query"])
                rows = db.execute(
                    f"SELECT * FROM messages WHERE {where} "
                    "ORDER BY created DESC,id DESC LIMIT 13 OFFSET ?",
                    [*params, args["offset"]],
                ).fetchall()
                more, rows = len(rows) > 12, rows[:12]
            return {
                "status": "ok" if rows else "no_matches",
                "messages": [
                    {
                        **self.archive.message_context(r, 1200),
                        "id": r["id"],
                        "scope": r["grp"],
                        "message_id": r["message_id"],
                        "sender": r["usr"],
                        "time": datetime.fromtimestamp(r["created"], TZ).isoformat(),
                        "text": r["content"][:1200],
                        "truncated": len(r["content"]) > 1200,
                    }
                    for r in rows
                ],
                "next_offset": args["offset"] + len(rows) if more else None,
            }
        except (ValueError, TypeError, OverflowError):
            return {
                "status": "invalid_arguments",
                "hint": "检查工具参数类型、时间格式和时间范围后重试",
            }


SEND_GROUP_FUNCTION = {
    "name": "send_group_message",
    "description": (
        "仅当当前私聊用户明确要求去群里发言时使用。自主拟一句符合人设的群聊发言，"
        "不披露私聊内容。目标不明确先list；不确定则询问。每次请求最多发一个群。"
    ),
    "parameters": {
        "type": "object",
        "properties": {"group_id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["group_id", "text"],
        "additionalProperties": False,
    },
}
