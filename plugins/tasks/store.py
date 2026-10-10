"""独立申请、任务及事件的事务台账。"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing

FILE = "data/tasks.sqlite3"
SCHEMA = """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, bot INTEGER NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, record TEXT NOT NULL, created REAL NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS events_record ON events(record,id);
"""


class Store:
    def __init__(self, root):
        path = root / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def all(self):
        return [
            json.loads(r[0]) for r in self.db.execute("SELECT payload FROM records ORDER BY rowid")
        ]

    def get(self, identity):
        row = self.db.execute("SELECT payload FROM records WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, record):
        record["updated"] = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO records VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (record["id"], record["bot"], json.dumps(record, ensure_ascii=False)),
            )

    def create(self, key, text, kind, application=False):
        now = time.time()
        record = dict(
            id=("a_" if application else "t_") + uuid.uuid4().hex[:16],
            bot=key[0],
            group=key[1],
            user=key[2],
            text=text,
            kind=kind,
            status="pending" if application else "queued",
            created=now,
            updated=now,
            supplements=[],
            delivered=[],
            notification="pending",
            summary="",
            phase="等待审核" if application else "排队中",
        )
        self.save(record)
        return record

    def approve(self, application, owner):
        # 同一事务内消费申请并创建执行任务，重复确认不会产生第二个任务。
        with self.db:
            task = dict(application)
            task.update(
                id="t_" + uuid.uuid4().hex[:16],
                status="queued",
                phase="排队中",
                approved_by=owner,
                application=application["id"],
                notification="pending",
            )
            application.update(status="approved", task=task["id"])
            for record in (application, task):
                self.db.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?)",
                    (record["id"], record["bot"], json.dumps(record, ensure_ascii=False)),
                )
        return task

    def event(self, identity, kind, data):
        with self.db:
            self.db.execute(
                "INSERT INTO events(record,created,kind,payload) VALUES(?,?,?,?)",
                (identity, time.time(), kind, json.dumps(data, ensure_ascii=False)),
            )

    def events(self, identity, limit=12):
        rows = self.db.execute(
            "SELECT created,kind,payload FROM events WHERE record=? ORDER BY id DESC LIMIT ?",
            (identity, limit),
        ).fetchall()
        return [dict(time=t, kind=k, data=json.loads(p)) for t, k, p in reversed(rows)]

    def close(self):
        self.db.close()


def accounts(path):
    """迁移前校验任务身份，拒绝机器人账号或工作目录标识伪造。"""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if set(
            db.execute(
                "SELECT type,name FROM sqlite_master WHERE type IN ('table','trigger','view')"
            )
        ) != {("table", "records"), ("table", "events")}:
            raise ValueError("任务台账结构无效")
        found = set()
        for identity, bot, payload in db.execute("SELECT id,bot,payload FROM records"):
            record = json.loads(payload)
            if not isinstance(record, dict):
                raise ValueError("任务台账记录无效")
            if (
                not re.fullmatch(r"[at]_[a-f0-9]{16}", identity)
                or record.get("id") != identity
                or record.get("bot") != bot
                or type(bot) is not int
                or bot <= 0
                or type(record.get("group")) is not int
                or record["group"] == 0
                or type(record.get("user")) is not int
                or record["user"] <= 0
            ):
                raise ValueError("任务台账身份无效")
            if (
                record.get("kind") not in ("task", "plugin")
                or record.get("status")
                not in (
                    "pending",
                    "approved",
                    "rejected",
                    "queued",
                    "running",
                    "testing",
                    "publishing",
                    "interrupted",
                    "cancelled",
                    "failed",
                    "completed",
                )
                or record.get("notification") not in ("pending", "attempting", "sent", "unknown")
                or any(not isinstance(record.get(k), str) for k in ("text", "phase", "summary"))
                or any(type(record.get(k)) not in (int, float) for k in ("created", "updated"))
                or not isinstance(record.get("supplements"), list)
                or not isinstance(record.get("delivered"), list)
                or any(
                    not isinstance(s, dict)
                    or not isinstance(s.get("id"), str)
                    or not isinstance(s.get("text"), str)
                    for s in record["supplements"]
                )
            ):
                raise ValueError("任务台账内容无效")
            found.add(bot)
        return found


def merge(local, incoming, output):
    """保留目标记录，导入执行任务必须显式继续；事件按内容去重。"""
    accounts(incoming)
    with closing(sqlite3.connect(output)) as dest:
        dest.executescript(SCHEMA)
        if local.exists():
            accounts(local)
            with closing(sqlite3.connect(local)) as db, dest:
                dest.executemany(
                    "INSERT INTO records VALUES(?,?,?)",
                    db.execute("SELECT id,bot,payload FROM records"),
                )
                dest.executemany(
                    "INSERT INTO events VALUES(?,?,?,?,?)",
                    db.execute("SELECT id,record,created,kind,payload FROM events"),
                )
        existing = {r[0] for r in dest.execute("SELECT id FROM records")}
        with closing(sqlite3.connect(incoming)) as src, dest:
            for identity, bot, payload in src.execute("SELECT id,bot,payload FROM records"):
                if identity in existing:
                    continue
                record = json.loads(payload)
                if identity.startswith("t_") and record["status"] in (
                    "queued",
                    "running",
                    "testing",
                    "publishing",
                ):
                    record.update(
                        status="interrupted",
                        phase="迁移后需明确补充要求以继续",
                        notification="sent",
                    )
                dest.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?)",
                    (identity, bot, json.dumps(record, ensure_ascii=False)),
                )
            known = set(dest.execute("SELECT record,created,kind,payload FROM events"))
            for event in src.execute("SELECT record,created,kind,payload FROM events"):
                if event not in known:
                    dest.execute(
                        "INSERT INTO events(record,created,kind,payload) VALUES(?,?,?,?)", event
                    )
                    known.add(event)
