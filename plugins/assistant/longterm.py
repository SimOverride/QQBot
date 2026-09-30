"""SQLite conversation archive and source-backed, self-reported user facts."""

import json
import re
import sqlite3
import time
from pathlib import Path

from .config import Config

FACT_FIELDS = {"称呼", "职业", "兴趣", "正在做的事", "交流偏好", "背景"}


class LongTermMemory:
    def __init__(self, path: Path, config: Config):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS directed_sends (
                bot INTEGER, usr INTEGER, message_id INTEGER, status TEXT,
                PRIMARY KEY(bot,usr,message_id)
            );
            CREATE TABLE IF NOT EXISTS disclosure_reviews (
                source_id INTEGER PRIMARY KEY, safe INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS share_attempts (
                source_id INTEGER PRIMARY KEY, bot INTEGER, grp INTEGER,
                usr INTEGER, created REAL, status TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot INTEGER NOT NULL, grp INTEGER NOT NULL, usr INTEGER NOT NULL,
                message_id INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
                created REAL NOT NULL,
                UNIQUE(bot, grp, message_id)
            );
            CREATE INDEX IF NOT EXISTS turns_user ON turns(bot, grp, usr, id DESC);
            CREATE TABLE IF NOT EXISTS facts (
                bot INTEGER NOT NULL, grp INTEGER NOT NULL, usr INTEGER NOT NULL,
                field TEXT NOT NULL, value TEXT NOT NULL, evidence TEXT NOT NULL,
                source_id INTEGER, updated REAL NOT NULL,
                PRIMARY KEY(bot, grp, usr, field)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot INTEGER NOT NULL, grp INTEGER NOT NULL, usr INTEGER NOT NULL,
                message_id INTEGER NOT NULL, content TEXT NOT NULL, types TEXT NOT NULL,
                created REAL NOT NULL, related_usr INTEGER, UNIQUE(bot,grp,message_id)
            );
            CREATE INDEX IF NOT EXISTS messages_time ON messages(bot,grp,created,id);
            CREATE INDEX IF NOT EXISTS messages_user ON messages(bot,grp,usr,id DESC);
            CREATE TABLE IF NOT EXISTS profile_cursor (
                bot INTEGER NOT NULL, grp INTEGER NOT NULL, usr INTEGER NOT NULL,
                last_id INTEGER NOT NULL,
                PRIMARY KEY(bot,grp,usr)
            );
        """)

        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "metadata" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")

    def message_context(self, row, limit=400):
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        reply = metadata.get("reply_to")
        if reply and reply.get("message_id") is not None:
            source = self.db.execute(
                "SELECT usr,content,metadata FROM messages WHERE bot=? AND grp=? AND message_id=?",
                (row["bot"], row["grp"], reply["message_id"]),
            ).fetchone()
            if source:
                source_metadata = json.loads(source["metadata"]) if source["metadata"] else {}
                reply = {
                    "message_id": reply["message_id"], "sender": source["usr"],
                    "sender_name": source_metadata.get("sender_name"),
                    "text": source["content"][:400],
                }
        return {
            "id": row["id"], "scope": row["grp"], "message_id": row["message_id"],
            "sender": row["usr"], "qq": row["usr"],
            "sender_name": metadata.get("sender_name"),
            "sender_role_at_send": metadata.get("sender_role_at_send"),
            "is_bot": row["usr"] == row["bot"],
            "mentions": metadata.get("mentions"),
            "reply_to": reply,
            "related_user": row["related_usr"],
            "response_to": metadata.get("response_to"),
            "text": row["content"][:limit], "time": row["created"],
            "truncated": len(row["content"]) > limit,
        }

    def close(self):
        self.db.close()

    def collect(
        self,
        key: tuple,
        message_id: int,
        content: str,
        types: list[str],
        created: float,
        related_user: int | None = None,
        metadata: dict | None = None,
    ):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO messages(bot,grp,usr,message_id,content,types,created,"
                "related_usr) VALUES(?,?,?,?,?,?,?,?)",
                (*key, message_id, content, json.dumps(types), created, related_user),
            )
            if metadata is not None:
                previous = self.db.execute(
                    "SELECT metadata FROM messages WHERE bot=? AND grp=? AND message_id=?",
                    (key[0], key[1], message_id),
                ).fetchone()[0]
                merged = json.loads(previous) if previous else {}
                merged.update({k: v for k, v in metadata.items() if v is not None})
                self.db.execute(
                    "UPDATE messages SET metadata=? WHERE bot=? AND grp=? AND message_id=?",
                    (json.dumps(merged, ensure_ascii=False), key[0], key[1], message_id),
                )
            if related_user is not None:
                self.db.execute(
                    "UPDATE messages SET related_usr=? WHERE bot=? AND grp=? AND message_id=?",
                    (related_user, key[0], key[1], message_id),
                )

    def cursor(self, key: tuple) -> int:
        row = self.db.execute(
            "SELECT last_id FROM profile_cursor WHERE bot=? AND grp=? AND usr=?",
            key,
        ).fetchone()
        return row[0] if row else 0

    def source_exists(self, key: tuple, source_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM messages WHERE bot=? AND grp=? AND usr=? AND id=?",
                (*key, source_id),
            ).fetchone()
            is not None
        )

    def candidates(self) -> list[tuple]:
        return [
            tuple(row)
            for row in self.db.execute(
                "SELECT m.bot,m.grp,m.usr FROM messages m LEFT JOIN profile_cursor c "
                "ON m.bot=c.bot AND m.grp=c.grp AND m.usr=c.usr "
                "WHERE m.id>COALESCE(c.last_id,0) AND m.usr<>m.bot "
                "GROUP BY m.bot,m.grp,m.usr HAVING COUNT(*)>=? ORDER BY MIN(m.id) LIMIT 2",
                (self.config.memory_profile_min_messages,),
            )
        ]

    def pending_text(self, key: tuple) -> tuple[str, int]:
        row = self.db.execute(
            "SELECT last_id FROM profile_cursor WHERE bot=? AND grp=? AND usr=?",
            key,
        ).fetchone()
        cursor = row[0] if row else 0
        rows = self.db.execute(
            "SELECT id,content FROM messages WHERE bot=? AND grp=? AND usr=? AND id>? "
            "ORDER BY id LIMIT 20",
            (*key, cursor),
        ).fetchall()
        texts, size, last = [], 0, cursor
        for row in rows:
            # Vision observations are not self-reported user facts.
            content = row["content"].split("\n[图片识别摘要", 1)[0][:2000]
            if size + len(content) > 8000:
                break
            last = row["id"]
            # Commands must not be turned back into facts after deletion/correction.
            if content.lstrip().startswith("/"):
                continue
            texts.append(content)
            size += len(content)
        return "\n".join(texts), last

    def advance(self, key: tuple, last_id: int):
        with self.db:
            self.db.execute(
                "INSERT INTO profile_cursor VALUES(?,?,?,?) ON CONFLICT(bot,grp,usr) "
                "DO UPDATE SET last_id=MAX(last_id,excluded.last_id)",
                (*key, last_id),
            )

    def seen(self, bot: int, group: int, message_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM turns WHERE bot=? AND grp=? AND message_id=?",
                (bot, group, message_id),
            ).fetchone()
            is not None
        )

    def save(self, key: tuple, message_id: int, question: str, answer: str) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO turns(bot,grp,usr,message_id,question,answer,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (*key, message_id, question, answer, time.time()),
            )
        return cursor.lastrowid

    def recent(self, key: tuple) -> tuple[list[dict], set[int]]:
        rows = self.db.execute(
            "SELECT * FROM turns WHERE bot=? AND grp=? AND usr=? ORDER BY id DESC LIMIT ?",
            (*key, self.config.session_max_turns),
        ).fetchall()
        kept, length = [], 0
        for row in rows:
            size = len(row["question"]) + len(row["answer"])
            if length + size > self.config.session_max_chars:
                break
            kept.append(row)
            length += size
        messages = [
            msg
            for row in reversed(kept)
            for msg in (
                {"role": "user", "content": row["question"]},
                {"role": "assistant", "content": row["answer"]},
            )
        ]
        return messages, {row["id"] for row in kept}

    def facts(self, key: tuple) -> list[dict]:
        rows = self.db.execute(
            "SELECT f.* FROM facts f WHERE f.bot=? AND f.usr=? ORDER BY f.updated DESC",
            (key[0], key[2]),
        ).fetchall()
        result = {}
        for row in rows:
            if row["grp"] != key[1]:
                if row["grp"] <= 0 and row["grp"] != -key[2]:
                    continue
                approved = self.db.execute(
                    "SELECT 1 FROM messages m JOIN disclosure_reviews r ON r.source_id=m.id "
                    "AND r.safe=1 WHERE m.bot=? AND m.grp=? AND m.usr=? "
                    "AND instr(m.content,?)>0 LIMIT 1",
                    (key[0], row["grp"], key[2], row["evidence"]),
                ).fetchone()
                if approved is None:
                    continue
            if row["field"] not in result:
                result[row["field"]] = {
                    k: row[k] for k in ("field", "value", "evidence", "updated")
                }
        return list(result.values())

    def context(self, key: tuple, query: str, exclude: set[int]) -> str:
        # Bounded lexical retrieval works with Chinese without another service or model.
        words = re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]+", query.lower())
        terms = set()
        for word in words:
            if re.fullmatch(r"[\u4e00-\u9fff]+", word):
                terms.update(word[i : i + 2] for i in range(len(word) - 1))
            else:
                terms.add(word)
        ranked = []
        rows = self.db.execute(
            "SELECT id,question,answer,created FROM turns WHERE bot=? AND grp=? AND usr=? "
            "ORDER BY id DESC LIMIT 1000",
            key,
        )
        for row in rows:
            if row["id"] in exclude:
                continue
            haystack = (row["question"] + row["answer"]).lower()
            score = sum(term in haystack for term in terms)
            if score:
                ranked.append((score, row["id"], row))
        older = []
        for _, _, row in sorted(ranked, key=lambda item: item[:2], reverse=True)[:3]:
            older.append(
                {
                    "time": row["created"],
                    "user": row["question"][:400],
                    "assistant": row["answer"][:400],
                }
            )
        rows = self.db.execute(
            "SELECT * FROM messages WHERE bot=? AND grp=? AND usr=? "
            "ORDER BY id DESC LIMIT 1000",
            key,
        ).fetchall()
        statements = [
            self.message_context(r, 200)
            for r in reversed(rows[:5])
            if not r["content"].lstrip().startswith("/")
        ]
        matches = sorted(
            (
                (sum(term in row["content"].lower() for term in terms), row["id"], row)
                for row in rows[5:]
                if not row["content"].lstrip().startswith("/")
            ),
            key=lambda item: item[:2],
            reverse=True,
        )
        relevant = [
            self.message_context(row)
            for score, _, row in matches[:3]
            if score > 0
        ]
        return json.dumps(
            {
                "subject_qq": key[2],
                "scope": key[1],
                "user_reported_facts": self.facts(key),
                "shared_person_knowledge": self.shared_knowledge(key),
                "related_history": older,
                "recent_user_group_messages": statements,
                "related_user_group_messages": relevant,
            },
            ensure_ascii=False,
        )

    def shared_knowledge(self, key: tuple) -> list[dict]:
        # Join live source rows: clearing originals immediately removes shared knowledge.
        rows = self.db.execute(
            "SELECT m.grp,m.content,m.created FROM messages m JOIN disclosure_reviews r "
            "ON r.source_id=m.id AND r.safe=1 WHERE m.bot=? AND m.usr=? "
            "ORDER BY m.id DESC LIMIT 100",
            (key[0], key[2]),
        ).fetchall()
        return [
            {
                "source": "私聊" if row["grp"] < 0 else "群聊",
                "text": row["content"],
                "time": row["created"],
            }
            for row in rows
            if row["grp"] > 0 or row["grp"] == -key[2]
        ][:12]

    def update_facts(self, key: tuple, source_id: int, user_text: str, updates: list[dict]):
        # Ignore model claims without a verbatim quote from this user's current message.
        with self.db:
            for item in updates[:6]:
                if not isinstance(item, dict):
                    continue
                field, value, evidence = (item.get(k) for k in ("field", "value", "evidence"))
                if not isinstance(field, str) or field not in FACT_FIELDS:
                    continue
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
                    continue
                if not isinstance(evidence, str) or not 2 <= len(evidence) <= 200:
                    continue
                if evidence not in user_text:
                    continue
                self.db.execute(
                    "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bot,grp,usr,field) "
                    "DO UPDATE SET value=excluded.value,evidence=excluded.evidence,"
                    "source_id=excluded.source_id,updated=excluded.updated",
                    (*key, field, value.strip(), evidence, source_id, time.time()),
                )

    def remember(self, key: tuple, field: str, value: str):
        if field not in FACT_FIELDS or not 1 <= len(value.strip()) <= 160:
            raise ValueError("字段：称呼、职业、兴趣、正在做的事、交流偏好、背景。内容1至160字。")
        with self.db:
            self.db.execute(
                "INSERT INTO facts VALUES(?,?,?,?,?,?,NULL,?) ON CONFLICT(bot,grp,usr,field) "
                "DO UPDATE SET value=excluded.value,evidence=excluded.evidence,"
                "source_id=NULL,updated=excluded.updated",
                (*key, field, value.strip(), "本人通过管理指令设置", time.time()),
            )
        last = self.db.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages WHERE bot=? AND grp=? AND usr=?",
            key,
        ).fetchone()[0]
        self.advance(key, last)

    def forget(self, key: tuple, field: str):
        if field not in FACT_FIELDS:
            raise ValueError("未知认知字段。")
        with self.db:
            self.db.execute(
                "DELETE FROM facts WHERE bot=? AND grp=? AND usr=? AND field=?", (*key, field)
            )
        # Skip existing unprocessed archive: only future messages may refresh the field.
        last = self.db.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages WHERE bot=? AND grp=? AND usr=?",
            key,
        ).fetchone()[0]
        self.advance(key, last)

    def delete(self, bot: int, group: int | None = None, user: int | None = None):
        clause, values = "bot=?", [bot]
        if group is not None:
            clause += " AND grp=?"
            values.append(group)
        if user is not None:
            clause += " AND usr=?"
            values.append(user)
        with self.db:
            for table in ("turns", "facts", "messages", "profile_cursor"):
                target_clause, target_values = clause, values
                if table == "messages" and user is not None:
                    target_clause = clause.replace("usr=?", "(usr=? OR related_usr=?)")
                    target_values = [*values, user]
                self.db.execute(f"DELETE FROM {table} WHERE {target_clause}", target_values)

    def count(self, key: tuple) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE bot=? AND grp=? AND usr=?", key
        ).fetchone()[0]
