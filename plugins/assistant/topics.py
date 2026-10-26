"""群话题归属、固定参与门槛和独立持久化话题日志。"""

import json
import sqlite3
import time
from pathlib import Path

from .trace import sanitize

DEFAULT_MIN_PARTICIPANTS = 2
MAX_MIN_PARTICIPANTS = 10


def validate_min_participants(value):
    """1至9为人数门槛，10表示10+且关闭主动回复。"""
    if type(value) is not int or not 1 <= value <= MAX_MIN_PARTICIPANTS:
        raise ValueError("接话参与人数须为1至10的整数，10表示10+（不主动回复）")


class Topics:
    def __init__(self, root, config):
        self.path = Path(root) / "logs/topics.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                bot INTEGER, grp INTEGER, start INTEGER, title TEXT, created REAL,
                updated REAL, replies INTEGER DEFAULT 0,
                PRIMARY KEY(bot,grp,start)
            );
            CREATE TABLE IF NOT EXISTS topic_messages (
                bot INTEGER, grp INTEGER, message_id INTEGER, topic INTEGER,
                source_id INTEGER, usr INTEGER, created REAL, payload TEXT,
                judgement TEXT, response_to INTEGER,
                PRIMARY KEY(bot,grp,message_id)
            );
            CREATE INDEX IF NOT EXISTS topic_chat ON topic_messages(bot,grp,topic,created);
            CREATE TABLE IF NOT EXISTS topic_decisions (
                id INTEGER PRIMARY KEY, bot INTEGER, grp INTEGER, topic INTEGER,
                message_id INTEGER, created REAL, allowed INTEGER, reason TEXT, outcome TEXT
            );
            CREATE TABLE IF NOT EXISTS topic_cursors (
                bot INTEGER, grp INTEGER, source_id INTEGER, PRIMARY KEY(bot,grp)
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(topics)")}
        if "attempted" in columns:
            # 评估次数不再限制接话，升级时移除旧阶段字段，保留话题及完整聊天。
            with self.db:
                self.db.execute("ALTER TABLE topics DROP COLUMN attempted")

    def close(self):
        self.db.close()

    def recent(self, bot, group):
        result = []
        for row in self.db.execute(
            "SELECT * FROM topics WHERE bot=? AND grp=? AND updated>? "
            "ORDER BY updated DESC LIMIT 12",
            (bot, group, time.time() - 900),
        ):
            item = dict(row)
            item["participants"] = [
                r[0]
                for r in self.db.execute(
                    "SELECT DISTINCT usr FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                    "AND usr<>?",
                    (bot, group, row["start"], bot),
                )
            ]
            item["messages"] = [
                {**json.loads(r[0]), "text": json.loads(r[0]).get("text", "")[:400]}
                for r in reversed(
                    self.db.execute(
                        "SELECT payload FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                        "ORDER BY created DESC,rowid DESC LIMIT 10",
                        (bot, group, row["start"]),
                    ).fetchall()
                )
            ]
            result.append(item)
        return result

    def cursor(self, bot, group):
        row = self.db.execute(
            "SELECT source_id FROM topic_cursors WHERE bot=? AND grp=?", (bot, group)
        ).fetchone()
        return row[0] if row else 0

    def record(self, bot, group, messages, assignments, *, advance=True):
        """整批校验通过后原子提交，模型不能将消息归到别群或尚未出现的话题。"""
        known = {
            row[0]
            for row in self.db.execute(
                "SELECT start FROM topics WHERE bot=? AND grp=? AND updated>?",
                (bot, group, time.time() - 900),
            )
        }
        if not isinstance(assignments, list) or len(assignments) != len(messages):
            raise ValueError("话题判断未覆盖整批消息")
        by_id = {m["message_id"]: m for m in messages}
        if len(by_id) != len(messages):
            raise ValueError("消息编号重复")
        checked = []
        for message, item in zip(messages, assignments, strict=True):
            if (
                not isinstance(item, dict)
                or type(item.get("message_id")) is not int
                or item["message_id"] != message["message_id"]
                or type(item.get("topic_start")) is not int
                or not isinstance(item.get("title"), str)
                or not 1 <= len(item["title"]) <= 200
                or not isinstance(item.get("judgement"), str)
                or not 1 <= len(item["judgement"]) <= 1000
                or type(item.get("should_reply")) is not bool
                or not isinstance(item.get("reply_reason"), str)
                or not 1 <= len(item["reply_reason"]) <= 1000
            ):
                raise ValueError("话题判断格式无效")
            start = item["topic_start"]
            if start not in known and start != message["message_id"]:
                raise ValueError("话题起始消息无效")
            known.add(start)
            target = item.get("response_to_bot")
            if target is not None:
                if type(target) is not int:
                    raise ValueError("机器人回应依据无效")
                source = self.db.execute(
                    "SELECT 1 FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                    "AND message_id=? AND usr=? AND source_id<?",
                    (bot, group, start, target, bot, message["id"]),
                ).fetchone()
                if source is None:
                    raise ValueError("回应依据不是本话题已发送的机器人消息")
            checked.append((message, item))
        with self.db:
            for message, item in checked:
                start = item["topic_start"]
                self.db.execute(
                    "INSERT OR IGNORE INTO topics(bot,grp,start,title,created,updated) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        bot,
                        group,
                        start,
                        sanitize(item["title"], self.config),
                        message["time"],
                        message["time"],
                    ),
                )
                self.db.execute(
                    "UPDATE topics SET updated=MAX(updated,?) WHERE bot=? AND grp=? AND start=?",
                    (message["time"], bot, group, start),
                )
                self.add_message(
                    bot, group, start, message, item["judgement"], item.get("response_to_bot")
                )
            if messages and advance:
                self.db.execute(
                    "INSERT OR REPLACE INTO topic_cursors VALUES(?,?,?)",
                    (bot, group, max(m["id"] for m in messages)),
                )

    def add_message(self, bot, group, topic, message, judgement, response_to=None):
        self.db.execute(
            "INSERT OR IGNORE INTO topic_messages VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                bot,
                group,
                message["message_id"],
                topic,
                message.get("id", 0),
                message["sender"],
                message["time"],
                json.dumps(sanitize(message, self.config), ensure_ascii=False),
                sanitize(judgement, self.config),
                response_to,
            ),
        )

    def gate(
        self,
        bot,
        group,
        message_id,
        assessment,
        explicit=False,
        *,
        min_participants=DEFAULT_MIN_PARTICIPANTS,
    ):
        validate_min_participants(min_participants)
        row = self.db.execute(
            "SELECT * FROM topic_messages WHERE bot=? AND grp=? AND message_id=?",
            (bot, group, message_id),
        ).fetchone()
        topic = self.db.execute(
            "SELECT * FROM topics WHERE bot=? AND grp=? AND start=?", (bot, group, row["topic"])
        ).fetchone()
        people = self.db.execute(
            "SELECT COUNT(DISTINCT usr) FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
            "AND usr<>?",
            (bot, group, topic["start"], bot),
        ).fetchone()[0]
        allowed = False
        handled = self.db.execute(
            "SELECT 1 FROM topic_decisions WHERE bot=? AND grp=? AND message_id=?",
            (bot, group, message_id),
        ).fetchone()
        if handled:
            reason = "本条消息已经处理，禁止重复触发"
        elif explicit:
            allowed, reason = True, "直接呼叫机器人，跳过人数及续话门槛"
        elif min_participants == MAX_MIN_PARTICIPANTS:
            reason = "本群参与人数设为10+，不主动回复"
        elif topic["replies"]:
            allowed = row["response_to"] is not None and assessment["should_reply"]
            reason = (
                "群友回应或讨论机器人已发送的说法：" + assessment["reply_reason"]
                if allowed
                else "机器人已接话，当前消息没有需要回应的机器人发言讨论"
            )
        elif people < min_participants:
            reason = f"新话题仅有{people}位群成员，等待至少{min_participants}人参与"
        else:
            allowed = assessment["should_reply"]
            reason = (
                f"新话题已有{people}位群成员参与（门槛{min_participants}人）："
                + assessment["reply_reason"]
            )
        with self.db:
            result = self.db.execute(
                "INSERT INTO topic_decisions"
                "(bot,grp,topic,message_id,created,allowed,reason,outcome) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    bot,
                    group,
                    topic["start"],
                    message_id,
                    time.time(),
                    allowed,
                    sanitize(reason, self.config),
                    "待执行" if allowed else "保持沉默",
                ),
            )
        return {
            "id": result.lastrowid,
            "start": topic["start"],
            "title": topic["title"],
            "participants": people,
            "min_participants": min_participants,
            "allowed": allowed,
            "reason": reason,
            "judgement": row["judgement"],
            "responded": bool(topic["replies"]),
        }

    def finish(self, decision, outcome):
        with self.db:
            self.db.execute("UPDATE topic_decisions SET outcome=? WHERE id=?", (outcome, decision))

    def sent(self, bot, group, topic, message, decision, first):
        with self.db:
            self.add_message(bot, group, topic, message, "机器人实际发送")
            self.db.execute(
                "UPDATE topics SET replies=replies+?,updated=MAX(updated,?) "
                "WHERE bot=? AND grp=? AND start=?",
                (int(first), time.time(), bot, group, topic),
            )
            self.db.execute("UPDATE topic_decisions SET outcome='已回复' WHERE id=?", (decision,))

    def prune(self):
        cutoff = time.time() - self.config.model_trace_retention_days * 86400
        with self.db:
            for table, field in (("topic_messages", "topic"), ("topic_decisions", "topic")):
                self.db.execute(
                    f"DELETE FROM {table} WHERE EXISTS (SELECT 1 FROM topics t "
                    f"WHERE t.bot={table}.bot AND t.grp={table}.grp AND t.start={table}.{field} "
                    "AND t.updated<?)",
                    (cutoff,),
                )
            self.db.execute("DELETE FROM topics WHERE updated<?", (cutoff,))
