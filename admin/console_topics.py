"""本机话题日志只读查询，列表仅返回有实际回复的话题。"""

import json
import sqlite3
from contextlib import contextmanager

from plugins.assistant.knowledge import read_directory


class TopicReader:
    def __init__(self, root):
        self.root = root

    @contextmanager
    def connect(self):
        path = self.root / "logs/topics.sqlite3"
        if not path.exists():
            yield None
            return
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def listing(self, bot=None, group=None, query="", offset=0):
        if offset < 0 or len(query) > 200:
            raise ValueError("话题筛选参数无效")
        with self.connect() as db:
            if db is None:
                return {"items": [], "more": False}
            where, args = ["replies>0"], []
            for key, value in (("bot", bot), ("grp", group)):
                if value is not None:
                    where.append(key + "=?")
                    args.append(value)
            if query:
                where.append("instr(title,?)>0")
                args.append(query)
            rows = db.execute(
                "SELECT * FROM topics WHERE "
                + " AND ".join(where)
                + " ORDER BY updated DESC,bot,grp,start LIMIT 51 OFFSET ?",
                (*args, offset),
            ).fetchall()
            directory = read_directory(self.root)
            items = []
            for row in rows[:50]:
                item = dict(row)
                item["group_name"] = (
                    directory.get(str(row["bot"]), {})
                    .get("groups", {})
                    .get(str(row["grp"]), {})
                    .get("name", str(row["grp"]))
                )
                items.append(item)
            return {"items": items, "more": len(rows) > 50}

    def detail(self, bot, group, start, offset=0):
        if offset < 0:
            raise ValueError("消息分页无效")
        with self.connect() as db:
            topic = (
                db.execute(
                    "SELECT * FROM topics WHERE bot=? AND grp=? AND start=? AND replies>0",
                    (bot, group, start),
                ).fetchone()
                if db
                else None
            )
            if topic is None:
                raise ValueError("话题不存在或尚未回复")
            rows = db.execute(
                "SELECT * FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                "ORDER BY created,rowid LIMIT 201 OFFSET ?",
                (bot, group, start, offset),
            ).fetchall()
            directory = read_directory(self.root).get(str(bot), {}).get("users", {})
            messages = []
            for row in rows[:200]:
                payload = json.loads(row["payload"])
                payload.update(
                    is_start=row["message_id"] == start,
                    is_self=row["usr"] == bot,
                    name=directory.get(str(row["usr"]), {}).get("name")
                    or payload.get("sender_name")
                    or str(row["usr"]),
                    judgement=row["judgement"],
                    response_to_bot=row["response_to"],
                )
                payload["decisions"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT created,allowed,reason,outcome FROM topic_decisions "
                        "WHERE bot=? AND grp=? AND topic=? AND message_id=? ORDER BY id",
                        (bot, group, start, row["message_id"]),
                    )
                ]
                messages.append(payload)
            return {"topic": dict(topic), "messages": messages, "more": len(rows) > 200}
