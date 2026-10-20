"""按机器人和群保存表情使用经验及停用状态，不提供删除能力。"""

import json
import sqlite3
import time
from contextlib import closing

from .emotes import tool

FUNCTION = tool(
    "manage_emote_memory",
    "读取或修正本群表情使用经验，或按反馈停用表情。没有删除能力。"
    "read返回版本，remember/disable须提交该版本及本群反馈消息编号；"
    "经验说明使用时机、实际反响和不确定性，不能将无回应视为负面反馈。",
    {
        "action": {"type": "string", "enum": ["read", "remember", "disable"]},
        "id": {"type": "string", "description": "表情文件ID"},
        "revision": {"type": "number"},
        "reason": {"type": "string"},
        "message_ids": {"type": "array", "items": {"type": "integer"}},
    },
)

SCHEMA = (
    "bot INTEGER NOT NULL, scope INTEGER NOT NULL, digest TEXT NOT NULL, "
    "disabled INTEGER NOT NULL, reason TEXT NOT NULL, memory TEXT NOT NULL, "
    "evidence TEXT NOT NULL, updated REAL NOT NULL, history TEXT NOT NULL, "
    "PRIMARY KEY(bot,scope,digest)"
)


def setup(db):
    db.execute("CREATE TABLE IF NOT EXISTS emote_group_state (" + SCHEMA + ")")


def states(library, digest=None):
    with closing(sqlite3.connect(library.memory_path)) as db:
        db.row_factory = sqlite3.Row
        setup(db)
        rows = db.execute(
            "SELECT * FROM emote_group_state" + (" WHERE digest=?" if digest else ""),
            (digest,) if digest else (),
        ).fetchall()
        return [dict(r) for r in rows]


def empty_state(bot, scope, digest):
    return dict(
        bot=bot,
        scope=scope,
        digest=digest,
        disabled=0,
        reason="",
        memory="",
        evidence="[]",
        updated=0,
        history="[]",
    )


def current(library, bot, scope, digest):
    return next(
        (r for r in states(library, digest) if r["bot"] == bot and r["scope"] == scope),
        empty_state(bot, scope, digest),
    )


def visible_state(archive, state):
    """原反馈删除或修改后不再把旧文本提供给模型，停用决定仍需人工恢复。"""
    result = {k: v for k, v in state.items() if k != "history"}
    evidence = json.loads(state.get("evidence", "[]"))
    valid = archive is not None and bool(evidence)
    for item in evidence:
        row = (
            archive.db.execute(
                "SELECT usr,content FROM messages WHERE bot=? AND grp=? AND message_id=?",
                (state["bot"], state["scope"], item["message_id"]),
            ).fetchone()
            if archive is not None
            else None
        )
        if row is None or row["usr"] != item.get("usr") or row["content"] != item.get("content"):
            valid = False
            break
    if not valid:
        result.update(
            memory="", reason="原反馈来源不可用" if state.get("disabled") else "", evidence="[]"
        )
    return result


def update(
    library, bot, scope, digest, revision, *, memory=None, disabled=None, reason=None, evidence=None
):
    """版本比较与写入在同一事务内；恢复只由后台调用。"""
    with closing(sqlite3.connect(library.memory_path)) as db, db:
        db.row_factory = sqlite3.Row
        setup(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM emote_group_state WHERE bot=? AND scope=? AND digest=?",
            (bot, scope, digest),
        ).fetchone()
        old = dict(row) if row else empty_state(bot, scope, digest)
        if old["updated"] != revision:
            raise ValueError("表情群状态已变化，请重新读取")
        history = json.loads(old["history"])
        if row:
            history.append({k: v for k, v in old.items() if k != "history"})
        value = {
            **old,
            "updated": max(time.time(), old["updated"] + 0.000001),
            "history": json.dumps(history, ensure_ascii=False),
        }
        for key, item in (("memory", memory), ("disabled", disabled), ("reason", reason)):
            if item is not None:
                value[key] = item
        if evidence is not None:
            value["evidence"] = json.dumps(evidence, ensure_ascii=False)
        db.execute(
            "INSERT OR REPLACE INTO emote_group_state VALUES(?,?,?,?,?,?,?,?,?)",
            tuple(
                value[k]
                for k in (
                    "bot",
                    "scope",
                    "digest",
                    "disabled",
                    "reason",
                    "memory",
                    "evidence",
                    "updated",
                    "history",
                )
            ),
        )
        return value


def execute(session, args):
    if not isinstance(args, dict) or set(args) != set(FUNCTION["parameters"]["required"]):
        raise ValueError
    bot, group, _ = session.key
    if group <= 0:
        return {"status": "group_only"}
    ident, action = args["id"], args["action"]
    if not isinstance(ident, str) or action not in ("read", "remember", "disable"):
        raise ValueError
    digest = session.emotes.digest(ident)
    state = current(session.emotes, bot, group, digest)
    if action == "read":
        recent = session.archive.db.execute(
            "SELECT * FROM messages WHERE bot=? AND grp=? AND usr=? "
            "AND content=? ORDER BY id DESC LIMIT 20",
            (bot, group, bot, f"[表情包：{ident}]"),
        ).fetchall()
        return {
            "status": "ok",
            **visible_state(session.archive, state),
            "recent_sends": [session.archive.message_context(r) for r in recent],
        }
    if (
        not isinstance(args["reason"], str)
        or not 1 <= len(args["reason"].strip()) <= 6000
        or not isinstance(args["message_ids"], list)
        or not args["message_ids"]
        or any(type(mid) is not int for mid in args["message_ids"])
    ):
        raise ValueError
    evidence = []
    for mid in dict.fromkeys(args["message_ids"]):
        row = session.archive.db.execute(
            "SELECT message_id,usr,content FROM messages WHERE bot=? AND grp=? AND message_id=?",
            (bot, group, mid),
        ).fetchone()
        if row is None or row["usr"] == bot:
            return {"status": "invalid_feedback"}
        evidence.append(dict(row))
    try:
        result = update(
            session.emotes,
            bot,
            group,
            digest,
            args["revision"],
            memory=args["reason"].strip() if action == "remember" else None,
            disabled=1 if action == "disable" else None,
            reason=args["reason"].strip() if action == "disable" else None,
            evidence=evidence,
        )
    except ValueError:
        return {"status": "conflict"}
    return {"status": "saved", **visible_state(session.archive, result)}
