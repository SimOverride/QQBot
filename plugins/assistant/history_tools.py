"""在请求权限范围内查询归档消息和认知。"""

from datetime import datetime, timedelta, timezone

from .prompt_store import prompt_text, tool_definition

TZ = timezone(timedelta(hours=8))
HISTORY_NAMES = (
    "list_sessions",
    "search_messages",
    "read_message_context",
    "resolve_entity",
    "read_knowledge",
)
HISTORY_FUNCTIONS = [tool_definition(name) for name in HISTORY_NAMES]


class HistoryTools:
    def __init__(self, archive, key, groups=(), send_callback=None, group_names=None):
        self.archive, self.key = archive, key
        self.send_callback = send_callback
        self.group_names = group_names or {}
        self.send_attempted = False
        self.emotes = None
        self.forward = None
        self.shares = None
        self.poke = None
        self.allow_silent = False
        self.extensions = None
        self.collector = None
        self.selected_emote = None
        self.selected_image = None
        self.selected_media = []
        self.offered_emotes = set()
        self.emote_hashes = {}
        self.scopes = {key[1]}
        if key[1] == -key[2]:
            self.scopes.update(g for g in groups if type(g) is int and g > 0)

    def instructions(self):
        return (
            prompt_text("history.overview")
            + "\n"
            + prompt_text(
                "history_tools.instructions.0",
                {"now": datetime.now(TZ).isoformat(), "bot_qq": self.key[0], "scope": self.key[1]},
            )
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

    def resolve_name(self, kind, query, scopes, offset):
        """按需查询获准会话中的名字；不返回整份目录，不授予新权限。"""
        from .knowledge import read_directory
        from .names import Names

        query = query.strip()
        if not query:
            return {"status": "invalid_arguments", "hint": "请输入完整昵称或群名"}
        root = self.archive.root
        names = Names(root, self.key[0])
        directory = read_directory(root).get(str(self.key[0]), {})
        groups = directory.get("groups", {})
        if kind == "group":
            matches = [
                {
                    "group_id": group,
                    "name": names.groups.get(group, self.group_names.get(group, "")),
                }
                for group in sorted(scopes)
                if group > 0
                and query.casefold()
                in {str(group), names.groups.get(group, self.group_names.get(group, "")).casefold()}
            ]
        else:
            members = {self.key[0], self.key[2]}
            membership = {}
            for scope in scopes:
                users = (
                    set(groups.get(str(scope), {}).get("members", []))
                    if scope > 0
                    else {self.key[2]}
                )
                users.update(
                    r[0]
                    for r in self.archive.db.execute(
                        "SELECT DISTINCT usr FROM messages WHERE bot=? AND grp=? "
                        "UNION SELECT DISTINCT usr FROM facts WHERE bot=? AND grp=?",
                        (self.key[0], scope, self.key[0], scope),
                    )
                )
                members.update(users)
                for user in users:
                    membership.setdefault(user, []).append(scope)
            matches = [
                {
                    "qq": user,
                    "name": names.names.get(user, ""),
                    "scopes": membership.get(user, []),
                    "is_self": user == self.key[0],
                }
                for user in sorted(members)
                if query.casefold()
                in {
                    str(user),
                    names.names.get(user, "").casefold(),
                    names.labels.get(user, "").casefold(),
                }
            ]
        return {
            "status": "ambiguous" if len(matches) > 1 else "ok" if matches else "not_found",
            "matches": matches[offset : offset + 10],
            "total": len(matches),
            "next_offset": offset + 10 if offset + 10 < len(matches) else None,
        }

    @staticmethod
    def timestamp(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError
        dt = datetime.fromisoformat(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp()

    def execute(self, name, args):
        try:
            if name not in HISTORY_NAMES or not isinstance(args, dict):
                raise ValueError
            schema = tool_definition(name)["parameters"]
            if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
                raise ValueError
            for key, value in args.items():
                rule = schema["properties"][key]
                types = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
                if not (
                    (value is None and "null" in types)
                    or (type(value) is int and "integer" in types)
                    or (isinstance(value, str) and "string" in types)
                ):
                    raise ValueError
                if isinstance(value, str) and not rule.get("minLength", 0) <= len(
                    value
                ) <= rule.get("maxLength", 20000):
                    raise ValueError
                if type(value) is int and not rule.get("minimum", -(10**20)) <= value <= rule.get(
                    "maximum", 10**20
                ):
                    raise ValueError
                if "enum" in rule and value not in rule["enum"]:
                    raise ValueError
            scope = args.get("scope", self.key[1])
            if scope is not None and scope not in self.scopes:
                return {"status": "access_denied"}
            scopes = sorted(
                self.scopes
                if scope is None or name in ("list_sessions", "read_message_context")
                else {scope}
            )
            offset = args.get("offset", 0)
            start, end = self.timestamp(args.get("start")), self.timestamp(args.get("end"))
            if start is not None and end is not None and start >= end:
                raise ValueError
            if name == "resolve_entity":
                return self.resolve_name(args["kind"], args["name"], scopes, offset)
            if name == "read_knowledge":
                from .prompt_store import group_knowledge

                kind = args.get("kind", "person")
                if kind == "group":
                    if "subject" in args:
                        raise ValueError
                    if self.key[1] <= 0:
                        return {"status": "access_denied"}
                    return {
                        "status": "ok",
                        "scope": self.key[1],
                        **group_knowledge(self.key[0], self.key[1], self.archive.root),
                    }
                subject = args.get("subject", self.key[2])
                if subject != self.key[2] and not any(
                    item["qq"] == subject
                    for item in self.resolve_name("user", str(subject), [self.key[1]], 0)["matches"]
                ):
                    return {"status": "access_denied"}
                key = (self.key[0], self.key[1], subject)
                return {
                    "status": "ok",
                    "scope": self.key[1],
                    "subject_qq": subject,
                    "facts": self.archive.facts(key),
                    "shared": self.archive.shared_knowledge(key)[:20],
                }
            where = "bot=? AND grp IN (" + ",".join("?" for _ in scopes) + ")"
            params = [self.key[0], *scopes]
            db = self.archive.db
            if name == "list_sessions":
                rows = db.execute(
                    "SELECT grp,MIN(created) AS first_record,MAX(created) AS last_record,"
                    f"COUNT(*) AS count FROM messages WHERE {where} GROUP BY grp",
                    params,
                ).fetchall()
                return {
                    "status": "ok",
                    "current_scope": self.key[1],
                    "allowed_scopes": scopes,
                    "group_names": {
                        g: name for g, name in self.group_names.items() if int(g) in scopes
                    },
                    "coverage": [dict(r) for r in rows],
                    "coverage_note": "仅为现存记录范围，不保证期间完整",
                }
            if name == "read_message_context":
                anchor = db.execute(
                    f"SELECT * FROM messages WHERE {where} AND id=?", [*params, args["id"]]
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
                    ("usr", "=", args.get("sender")),
                    ("created", ">=", start),
                    ("created", "<", end),
                ):
                    if value is not None:
                        where += f" AND {column}{op}?"
                        params.append(value)
                if args.get("query", ""):
                    where += " AND instr(lower(content),lower(?))>0"
                    params.append(args.get("query", ""))
                rows = db.execute(
                    f"SELECT * FROM messages WHERE {where} "
                    "ORDER BY created DESC,id DESC LIMIT 13 OFFSET ?",
                    [*params, offset],
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
                        "text": r["content"],
                        "truncated": False,
                    }
                    for r in rows
                ],
                "next_offset": offset + len(rows) if more else None,
            }
        except (ValueError, TypeError, OverflowError):
            return {
                "status": "invalid_arguments",
                "hint": "检查工具参数类型、时间格式和时间范围后重试",
            }


SEND_GROUP_FUNCTION = tool_definition("send_group_message")
