"""主 agent 根据消息依据维护会话认知，保护人工固定值并检查版本。"""

import hashlib
import json
import time

from .consolidation import (
    FIXED,
    Consolidation,
    buckets,
    fingerprint,
    group_source,
    label,
    organized,
)
from .knowledge import GROUP_FIELDS
from .prompt_store import group_knowledge, read_console, tool_definition

FUNCTION = tool_definition("manage_memory")


def execute(session, args):
    import migration

    with migration.project_lock(session.archive.root, name=".migration.lock"):
        if (session.archive.root / migration.JOURNAL).exists():
            return {"status": "migration_in_progress"}
        return _execute(session, args)


def _execute(session, args):
    if not isinstance(args, dict) or set(args) != set(FUNCTION["parameters"]["required"]):
        raise ValueError
    bot, scope, user = session.key
    subject, field = args["subject"], args["field"]
    if type(subject) is not int or not isinstance(field, str):
        raise ValueError
    group = subject == 0
    if group:
        if scope <= 0 or field not in ("summary", "knowledge"):
            return {"status": "invalid_scope"}
    elif subject <= 0 or subject == bot or (scope < 0 and subject != user):
        return {"status": "invalid_subject"}
    elif field not in ("总体认知", *(GROUP_FIELDS if scope > 0 else ("会话印象",))):
        return {"status": "invalid_field"}
    archive = session.archive
    cache = Consolidation(archive, archive.root, session.archive.config)
    db = archive.db
    target_scope = 0 if field == "总体认知" else scope
    name = f"group:{bot}:{scope}" if group else label((bot, target_scope, subject, field))

    def snapshot():
        if group:
            fixed = read_console(archive.root).get("groups", {}).get(f"{bot}:{scope}")
            source = group_source(db, bot, scope)
            value = {"summary": "", "knowledge": "", **group_knowledge(bot, scope, archive.root)}
            return source, value, fixed is not None
        rows = db.execute(
            "SELECT * FROM facts WHERE bot=? AND grp=? AND usr=? ORDER BY field",
            (bot, target_scope, subject),
        ).fetchall()
        source = buckets(rows).get((bot, target_scope, subject, field), [])
        if target_scope == 0:
            source = [
                r
                for r in source
                if r["evidence"].startswith(FIXED)
                or db.execute(
                    "SELECT 1 FROM messages WHERE bot=? AND grp=? AND id=?",
                    (bot, scope, r["source_id"]),
                ).fetchone()
            ]
        value = "\n".join(dict.fromkeys(r["value"] for r in organized(source, archive.root)))
        return source, value, any(r["evidence"].startswith(FIXED) for r in source)

    source, value, fixed = snapshot()
    version = fingerprint([source, value, fixed])
    if args["action"] == "read":
        return {
            "status": "ok",
            "revision": version,
            "value": value,
            "fixed": fixed,
            "sources": source,
        }
    if args["action"] != "write" or not isinstance(args["value"], str):
        raise ValueError
    if fixed:
        return {"status": "fixed"}
    if version != args["revision"]:
        return {"status": "conflict"}
    if not 1 <= len(args["value"].strip()) <= (6000 if group else 12000):
        raise ValueError
    ids = args["sources"]
    if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
        raise ValueError
    evidence = []
    for ident in dict.fromkeys(ids):
        row = db.execute(
            "SELECT * FROM messages WHERE bot=? AND grp=? AND id=?", (bot, scope, ident)
        ).fetchone()
        if (
            row is None
            or row["usr"] == bot
            or (not group and row["usr"] != subject)
            or json.loads(row["metadata"] or "{}").get("poke")
        ):
            return {"status": "invalid_evidence"}
        evidence.append(dict(row))
    original_source = source
    now = time.time()
    with db:
        # 写入期间重新核验固定内容和来源，清除历史后的旧回合不能重建资料。
        latest = snapshot()
        if fingerprint(list(latest)) != version:
            return {"status": "conflict"}
        if not group:
            for previous_row in source:
                db.execute(
                    "DELETE FROM facts WHERE bot=? AND grp=? AND usr=? AND field=?",
                    (bot, target_scope, subject, previous_row["field"]),
                )
            for row in evidence:
                ident = hashlib.sha256(f"{scope}:{row['message_id']}:{field}".encode()).hexdigest()
                db.execute(
                    "INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)",
                    (
                        bot,
                        target_scope,
                        subject,
                        field + ":" + ident,
                        row["content"],
                        row["content"],
                        row["id"],
                        now,
                    ),
                )
            source = snapshot()[0]
        else:
            by_id = {r["id"]: r for r in source}
            for r in evidence:
                by_id[r["id"]] = {
                    k: r[k] for k in ("id", "message_id", "usr", "content", "created")
                }
            source = sorted(by_id.values(), key=lambda r: r["id"])
        previous = cache.cache.get(name, {})
        history = list(previous.get("history", []))
        history.append({"value": value, "source": original_source, "updated": now})
        cache.cache[name] = {
            "value": {**value, field: args["value"].strip()} if group else args["value"].strip(),
            "source": source,
            "fingerprint": fingerprint(source),
            "updated": now,
            "history": history,
        }
        cache.save()
    return {"status": "saved"}
