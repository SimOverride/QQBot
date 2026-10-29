"""统一记忆工具：共享读取，证据检查，版本冲突与人工固定保护。"""

import json
from datetime import datetime, timedelta, timezone

from .prompt_store import tool_definition
from .vault import FIELDS, Vault, source

FUNCTION = tool_definition("manage_memory")


def execute(session, args):
    import migration

    with migration.project_lock(session.archive.root, name=".migration.lock"):
        if (session.archive.root / migration.JOURNAL).exists():
            return {"status": "migration_in_progress"}
        if not getattr(session, "is_current", lambda: True)():
            return {"status": "superseded"}
        return _execute(session, args)


def _execute(session, args):
    if not isinstance(args, dict) or set(args) != set(FUNCTION["parameters"]["required"]):
        raise ValueError("记忆参数无效")
    bot, scope, _ = session.key
    subject, field = args["subject"], args["field"]
    if type(subject) is not int or subject < 0:
        return {"status": "invalid_subject"}
    if field not in (*FIELDS, "summary", "knowledge", "自我档案", "想法", "日记"):
        return {"status": "invalid_field"}
    kind = {
        "summary": "context",
        "knowledge": "context",
        "自我档案": "self",
        "想法": "thought",
        "日记": "diary",
    }.get(field, "impression")
    if kind in ("self", "thought", "diary"):
        subject = bot
    if kind == "diary":
        field += ":" + datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    if kind == "context":
        subject = 0
        field = f"{scope}:{field}"
    vault = Vault(session.archive.root, bot)
    old = vault.field(subject, field, kind)
    current = {
        "status": "ok",
        "revision": old["revision"] if old else "",
        "value": old["value"] if old and vault.valid(old) else "",
        "fixed": bool(old and old.get("fixed")),
        "sources": old.get("sources", []) if old else [],
    }
    if args["action"] == "read":
        return current
    if args["action"] != "write" or not isinstance(args["value"], str):
        raise ValueError("记忆操作无效")
    if current["fixed"]:
        return {"status": "fixed"}
    if current["revision"] != args["revision"]:
        return {"status": "conflict"}
    if kind == "diary" and old and len(old.get("history", [])) >= 2:
        return {"status": "daily_limit", "hint": "当日日记最多保存三次，避免重复自我强化。"}
    if not 1 <= len(args["value"].strip()) <= 12000:
        raise ValueError("记忆内容须为 1 至 12000 字")
    ids = args["sources"]
    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
        raise ValueError("来源编号无效")
    if not ids and kind not in ("self", "thought", "diary"):
        return {"status": "invalid_evidence"}
    evidence = []
    for ident in dict.fromkeys(ids):
        row = session.archive.db.execute(
            "SELECT * FROM messages WHERE bot=? AND id=?", (bot, ident)
        ).fetchone()
        if (
            row is None
            or row["usr"] == bot
            or (kind == "impression" and row["usr"] != subject)
            or json.loads(row["metadata"] or "{}").get("poke")
        ):
            return {"status": "invalid_evidence"}
        evidence.append(source(row))
    saved = vault.set_field(
        subject,
        field,
        args["value"],
        sources=evidence,
        expected=old["revision"] if old else None,
        kind=kind,
    )
    return {"status": "saved", "id": saved["id"], "revision": saved["revision"]}
