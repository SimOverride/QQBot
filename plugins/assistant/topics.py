"""群话题归属、固定参与门槛和独立持久化话题日志。"""

import json
import sqlite3
import time
from pathlib import Path

from .trace import sanitize

DEFAULT_MIN_PARTICIPANTS = 2
MAX_MIN_PARTICIPANTS = 10


class TopicError(ValueError):
    """只携带固定校验类别，不将模型输出或聊天正文放入异常。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


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

    def recent(self, bot, group, *, before=None, at=None):
        """模型只看到平台编号；单条重判按归档顺序隐藏其后的消息。"""
        result = []
        for row in self.db.execute(
            "SELECT t.*,MAX(m.created) AS observed_at FROM topics t "
            "JOIN topic_messages m ON m.bot=t.bot AND m.grp=t.grp AND m.topic=t.start "
            "WHERE t.bot=? AND t.grp=? AND (? IS NULL OR m.source_id<?) "
            "GROUP BY t.bot,t.grp,t.start HAVING MAX(m.created)>? "
            "ORDER BY observed_at DESC,t.start LIMIT 12",
            (bot, group, before, before, (time.time() if at is None else at) - 900),
        ):
            item = {"start": row["start"], "title": row["title"]}
            item["participants"] = [
                r[0]
                for r in self.db.execute(
                    "SELECT DISTINCT usr FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                    "AND usr<>? AND (? IS NULL OR source_id<?)",
                    (bot, group, row["start"], bot, before, before),
                )
            ]
            item["replies"] = (
                row["replies"]
                if before is None
                else int(
                    self.db.execute(
                        "SELECT 1 FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                        "AND usr=? AND source_id<? LIMIT 1",
                        (bot, group, row["start"], bot, before),
                    ).fetchone()
                    is not None
                )
            )
            item["messages"] = [
                {k: v[:400] if k == "text" else v for k, v in json.loads(r[0]).items() if k != "id"}
                for r in reversed(
                    self.db.execute(
                        "SELECT payload FROM topic_messages WHERE bot=? AND grp=? AND topic=? "
                        "AND (? IS NULL OR source_id<?) "
                        "ORDER BY source_id DESC,rowid DESC LIMIT 10",
                        (bot, group, row["start"], before, before),
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

    def record(self, bot, group, message, item, *, advance=True):
        """校验并原子保存单条消息，归档顺序只用于内部因果验证。"""
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
            raise TopicError("assignment_format")
        start = item["topic_start"]
        if start != message["message_id"]:
            origin = self.db.execute(
                "SELECT m.source_id FROM topics t JOIN topic_messages m "
                "ON m.bot=t.bot AND m.grp=t.grp AND m.topic=t.start AND m.message_id=t.start "
                "WHERE t.bot=? AND t.grp=? AND t.start=?",
                (bot, group, start),
            ).fetchone()
            if origin is None:
                raise TopicError("unknown_topic")
            if origin[0] > message["id"]:
                raise TopicError("topic_after_message")
        target = item.get("response_to_bot")
        if target is not None:
            if type(target) is not int:
                raise TopicError("response_format")
            source = self.db.execute(
                "SELECT topic,usr,source_id FROM topic_messages "
                "WHERE bot=? AND grp=? AND message_id=?",
                (bot, group, target),
            ).fetchone()
            if source is None:
                raise TopicError("unknown_response")
            if source["usr"] != bot:
                raise TopicError("response_not_bot")
            if source["topic"] != start:
                raise TopicError("response_other_topic")
            if source["source_id"] >= message["id"]:
                raise TopicError("response_after_message")
        with self.db:
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
            self.add_message(bot, group, start, message, item["judgement"], target)
            if advance:
                self.db.execute(
                    "INSERT OR REPLACE INTO topic_cursors VALUES(?,?,?)",
                    (bot, group, message["id"]),
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
