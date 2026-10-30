"""模型调用的表情经验存储与发送参考，不执行语义规则判断。"""

import json
import re
import sqlite3
import time
from contextlib import closing

from .vault import Vault, digest, identifier, source

SEND_SCHEMA = (
    "id TEXT PRIMARY KEY, bot INTEGER NOT NULL, scope INTEGER NOT NULL, "
    "digest TEXT NOT NULL, message_id INTEGER NOT NULL, subject INTEGER NOT NULL, "
    "context TEXT NOT NULL, created REAL NOT NULL"
)


def setup(db):
    db.execute("CREATE TABLE IF NOT EXISTS emote_sends (" + SEND_SCHEMA + ")")


def sent(library, bot, scope, image_digest, message_id, subject, context):
    if type(message_id) is not int or type(subject) is not int or subject < 0:
        return
    with closing(sqlite3.connect(library.memory_path)) as db, db:
        setup(db)
        db.execute(
            "INSERT OR IGNORE INTO emote_sends VALUES(?,?,?,?,?,?,?,?)",
            (
                digest([bot, scope, message_id, image_digest]),
                bot,
                scope,
                image_digest,
                message_id,
                subject,
                str(context)[:1000],
                time.time(),
            ),
        )


def feedback_note(library, bot, image_digest, subject):
    vault = Vault(library.project_root, bot)
    return vault.get(identifier(subject, "表情反馈:" + image_digest, "emote"))


def recent_sends(library, bot, image_digest):
    """发送流水供模型阅读，不据时间或回复关系限制反馈写入。"""
    with closing(sqlite3.connect(library.memory_path)) as db:
        db.row_factory = sqlite3.Row
        setup(db)
        return [
            dict(row)
            for row in db.execute(
                "SELECT scope,message_id,subject,context,created FROM emote_sends "
                "WHERE bot=? AND digest=? ORDER BY created DESC LIMIT 30",
                (bot, image_digest),
            )
        ]


def save_feedback(
    library, archive, bot, subject, image_digest, value, sentiment, disabled, revision, message_ids
):
    """模型决定反馈含义与归属；程序只校验来源身份、版本及存储参数。"""
    vault = Vault(archive.root, bot)
    ident = identifier(subject, "表情反馈:" + image_digest, "emote")
    old = vault.get(ident)
    if revision != (old["revision"] if old else ""):
        return {"status": "conflict"}
    evidence = []
    for message_id in dict.fromkeys(message_ids):
        row = archive.db.execute(
            "SELECT * FROM messages WHERE bot=? AND id=?", (bot, message_id)
        ).fetchone()
        if row is None:
            return {"status": "invalid_evidence"}
        evidence.append(source(row))
    activity_events = []
    note = vault.put(
        {
            "id": ident,
            "kind": "emote",
            "subject": subject,
            "field": "表情反馈:" + image_digest,
            "image_digest": image_digest,
            "title": f"{'全局' if subject == 0 else subject} · 表情使用经验",
            "value": value,
            "sources": evidence,
            "authored": "model",
            "sentiment": sentiment,
            "disabled": disabled,
        },
        old["revision"] if old else None,
        activity_events=activity_events,
    )
    from .memory_activity import emit

    emit(
        archive.root,
        bot,
        "emote_feedback",
        notes=[note["id"]],
        result="saved",
        sentiment=sentiment,
        change_event_ids=activity_events,
    )
    return {"status": "saved", "id": note["id"], "revision": note["revision"]}


def migrate_legacy(library, archive, bot):
    """旧群经验只转为待审笔记，不伪造发送关联或扩大旧群禁用范围。"""
    if library is None or not library.memory_path.exists():
        return 0
    with closing(
        sqlite3.connect(library.memory_path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='emote_group_state'"
            ).fetchone()
            is None
        ):
            return 0
        rows = [dict(r) for r in db.execute("SELECT * FROM emote_group_state WHERE bot=?", (bot,))]
    vault = Vault(archive.root, bot)
    count = 0
    for old in rows:
        image_digest = old["digest"]
        if not isinstance(image_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", image_digest):
            continue
        field = f"旧群表情经验:{old['scope']}:{image_digest}"
        ident = identifier(0, field, "emote")
        if vault.get(ident):
            continue
        refs = []
        try:
            evidence = json.loads(old["evidence"])
        except (TypeError, ValueError):
            evidence = []
        for item in evidence if isinstance(evidence, list) else []:
            if not isinstance(item, dict):
                continue
            row = archive.db.execute(
                "SELECT * FROM messages WHERE bot=? AND grp=? AND usr=? AND message_id=?",
                (bot, old["scope"], item.get("usr"), item.get("message_id")),
            ).fetchone()
            if row and row["content"] == item.get("content"):
                refs.append(source(row))
        vault.put(
            {
                "id": ident,
                "kind": "emote",
                "subject": 0,
                "field": field,
                "image_digest": image_digest,
                "title": f"待审 · 原群 {old['scope']} 表情经验",
                "value": (
                    f"原群：{old['scope']}\n旧使用经验：{old['memory']}\n旧原因：{old['reason']}\n"
                    "此记录缺少可核验的成功发送关联，不能自动作为跨群禁用或反馈。"
                ),
                "state": "quarantine",
                "sources": refs,
                "legacy_group_state": old,
                "disabled": False,
            }
        )
        count += 1
    return count
