"""表情反馈归因：只学习可追溯的成功发送，不从沉默推断反响。"""

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


def associated(library, row, image_digest=None):
    """明确引用优先；无引用仅接收短时间内唯一发送的明确表情指代。"""
    metadata = json.loads(row["metadata"] or "{}")
    reply_id = (metadata.get("reply_to") or {}).get("message_id")
    with closing(sqlite3.connect(library.memory_path)) as db:
        db.row_factory = sqlite3.Row
        setup(db)
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM emote_sends WHERE bot=? AND scope=? "
                "AND created<=? ORDER BY created DESC LIMIT 30",
                (row["bot"], row["grp"], row["created"] + 1),
            )
        ]
    if reply_id is not None:
        rows = [r for r in rows if r["message_id"] == reply_id]
    else:
        if not re.search(r"这[张个幅]?(?:表情|图)|刚[才刚].*(?:表情|图)", row["content"]):
            return None
        rows = [
            r
            for r in rows
            if -1 <= row["created"] - r["created"] <= 120 and r["subject"] == row["usr"]
        ]
    if len(rows) != 1 or (image_digest and rows[0]["digest"] != image_digest):
        return None
    return rows[0]


def verdict(text):
    """保守识别直接反响；含否定的好评、引用及不确定表达留给模型核实。"""
    if any(
        word in text
        for word in ("如果", "假如", "比如", "他说", "她说", "“", "「", "『", '"', "```")
    ) or re.search(r"(?m)^\s*>|不是不|并非不|没有不|不讨厌|难道|[？?]", text):
        return None
    if re.search(r"可以再发|可以发了|恢复使用|以后可以发", text):
        return "allow", False
    if re.search(r"别再发|不要再发|以后别用|以后别发|别给我发|不要给我发|别用了", text):
        return "avoid", True
    if re.search(r"不合适|不适合|讨厌|冒犯|很烦|不好笑|不喜欢", text):
        return "negative", False
    if re.search(r"不|没|别|并非|不是|未必", text):
        return None
    if re.search(r"喜欢|合适|好笑|可爱|有趣|贴切|好用|哈哈", text):
        return "positive", False
    return None


def feedback_note(library, bot, image_digest, subject):
    vault = Vault(library.project_root, bot)
    return vault.get(identifier(subject, "表情反馈:" + image_digest, "emote"))


def save_feedback(library, archive, row, send, value, sentiment, disabled=False, revision=None):
    """同一发送者可修正自己的经验，固定记录、旧消息及重复消息不覆盖当前认识。"""
    vault = Vault(archive.root, row["bot"])
    ident = identifier(row["usr"], "表情反馈:" + send["digest"], "emote")
    old = vault.get(ident)
    current = archive.db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
    sent_row = archive.db.execute(
        "SELECT * FROM messages WHERE bot=? AND grp=? AND usr=? AND message_id=?",
        (row["bot"], row["grp"], row["bot"], send["message_id"]),
    ).fetchone()
    if (
        current is None
        or source(current) != source(row)
        or sent_row is None
        or sent_row["id"] >= row["id"]
    ):
        return {"status": "source_removed"}
    if old and old.get("fixed"):
        return {"status": "fixed"}
    if revision is not None and revision != (old["revision"] if old else ""):
        return {"status": "conflict"}
    ref = source(row)
    if old and ref in old.get("sources", []):
        return {"status": "unchanged", "id": ident}
    if old and any(s["created"] > row["created"] for s in old.get("sources", [])):
        return {"status": "older_feedback"}
    activity_events = []
    note = vault.put(
        {
            "id": ident,
            "kind": "emote",
            "subject": row["usr"],
            "field": "表情反馈:" + send["digest"],
            "image_digest": send["digest"],
            "title": f"{row['usr']} · 表情使用反馈",
            "value": value,
            "sources": [ref, source(sent_row)],
            "sentiment": sentiment,
            "disabled": disabled or bool(old and old.get("disabled") and sentiment != "allow"),
            "send": send,
        },
        old["revision"] if old else None,
        activity_events=activity_events,
    )
    from .memory_activity import emit

    emit(
        archive.root,
        row["bot"],
        "emote_feedback",
        notes=[note["id"]],
        result="saved",
        sentiment=sentiment,
        change_event_ids=activity_events,
    )
    return {"status": "saved", "id": note["id"], "revision": note["revision"]}


def collect_feedback(archive, row, library):
    if library is None or row["usr"] == row["bot"]:
        return {"status": "not_feedback"}
    send = associated(library, row)
    decision = verdict(row["content"])
    if send is None or decision is None:
        return {"status": "not_feedback"}
    sentiment, disabled = decision
    result = save_feedback(
        library,
        archive,
        row,
        send,
        f"用户 {row['usr']} 对实际发送表情的反馈：{row['content'][:1200]}\n"
        f"发送时语境：{send['context'] or '未记录'}\n"
        "适用范围：该用户的反馈，不代表其他人的偏好。",
        sentiment,
        disabled,
    )
    return result


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
