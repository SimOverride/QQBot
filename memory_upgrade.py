"""离线统一记忆升级；预演不修改原库，发布使用现有恢复日志。"""

import math
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import migration
from plugins.assistant.consolidation import FIXED, organized
from plugins.assistant.vault import Vault, identifier, source

MARKER = "data/memory-upgraded.json"


def required(root):
    if (root / MARKER).exists():
        return False
    if (root / "data/knowledge-cleanup.json").exists():
        return True
    for name, field in (("data/console.json", "groups"), ("group_chat.json", "groups")):
        path = root / name
        if path.exists():
            entries = migration.read_json(path).get(field, {})
            if name.endswith("console.json") and entries:
                return True
            if any("persona" in item for item in entries.values()):
                return True
    path = root / migration.MEMORY
    if not path.exists():
        return False
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return bool(db.execute("SELECT 1 FROM facts LIMIT 1").fetchone())
    finally:
        db.close()


def upgrade(root, *, apply=False):
    root = Path(root)
    with migration.project_lock(root), migration.project_lock(root, name=".migration.lock"):
        if (root / migration.JOURNAL).exists():
            raise ValueError("请先恢复未完成迁移")
        if (root / MARKER).exists():
            return {"state": "already_upgraded"}
        with tempfile.TemporaryDirectory(prefix="qqbot-memory-upgrade-") as temporary:
            staged = Path(temporary)
            original_db = root / migration.MEMORY
            read_db = original_db
            if not original_db.exists():
                from plugins.assistant.config import Config
                from plugins.assistant.longterm import LongTermMemory

                read_db = staged / "empty.sqlite3"
                empty = LongTermMemory(read_db, Config())
                empty.close()
            db = sqlite3.connect(read_db.resolve().as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute("SELECT * FROM facts ORDER BY bot,usr,grp,field").fetchall()
                groups = defaultdict(list)
                for row in organized(rows, root):
                    groups[(row["bot"], row["usr"], row["field"].split(":", 1)[0])].append(
                        dict(row)
                    )
                counts = {"notes": 0, "quarantine": 0}
                for (bot, user, field), entries in groups.items():
                    refs, values = [], []
                    fixed = [r for r in entries if r["evidence"].startswith(FIXED)]
                    selected = fixed or entries
                    originals = [
                        dict(r)
                        for r in rows
                        if r["bot"] == bot
                        and r["usr"] == user
                        and r["field"].split(":", 1)[0] == field
                    ]
                    for row in selected:
                        if row["value"] not in values:
                            values.append(row["value"])
                    missing_source = False
                    for row in originals:
                        if row["source_id"] is not None:
                            original = db.execute(
                                "SELECT * FROM messages WHERE id=? AND bot=?",
                                (row["source_id"], bot),
                            ).fetchone()
                            if original:
                                refs.append(source(original))
                            elif not row["evidence"].startswith(FIXED):
                                missing_source = True
                        elif not row["evidence"].startswith(FIXED):
                            missing_source = True
                    conflict = bool(fixed and len(values) > 1) or (
                        not fixed and (not refs or missing_source)
                    )
                    Vault(staged, bot).put(
                        {
                            "id": identifier(user, field),
                            "subject": user,
                            "field": field,
                            "kind": "impression",
                            "value": "\n".join(values),
                            "sources": refs,
                            "fixed": bool(fixed),
                            "state": "quarantine" if conflict else "active",
                            "legacy_sources": originals,
                            "legacy_views": entries,
                        },
                        manual=True,
                    )
                    counts["notes"] += 1
                    counts["quarantine"] += int(conflict)
                console_path = root / "data/console.json"
                console = migration.read_json(console_path) if console_path.exists() else {}
                from plugins.assistant.consolidation import automatic_group, read

                keys = set(console.get("groups", {}))
                keys.update(k.removeprefix("group:") for k in read(root) if k.startswith("group:"))
                for key in keys:
                    bot, group = map(int, key.split(":"))
                    fixed = key in console.get("groups", {})
                    values = (
                        console.get("groups", {}).get(key)
                        if fixed
                        else automatic_group(root, bot, group)
                    )
                    for field, value in (values or {}).items():
                        if field not in ("summary", "knowledge"):
                            continue
                        refs = []
                        cached = read(root).get(f"group:{key}", {})
                        if not fixed:
                            for item in cached.get("source", []):
                                row = db.execute(
                                    "SELECT * FROM messages WHERE bot=? AND grp=? AND id=?",
                                    (bot, group, item["id"]),
                                ).fetchone()
                                if row is None:
                                    raise ValueError("群认知来源在预演中发生变化")
                                refs.append(source(row))
                        Vault(staged, bot).set_field(
                            0, f"{group}:{field}", value, sources=refs, manual=fixed, kind="context"
                        )
                        counts["notes"] += 1
                if not apply:
                    return {"state": "preview", **counts}
                migration.snapshot(read_db, staged / "memory.sqlite3")
            finally:
                db.close()
            (staged / "empty.sqlite3").unlink(missing_ok=True)
            # 原始旧库保存在回滚目录；新库停止旧认知写入。
            with closing(sqlite3.connect(staged / "memory.sqlite3")) as clean, clean:
                clean.execute("DELETE FROM facts")
            (staged / "data").mkdir(exist_ok=True)
            shutil.move(staged / "memory.sqlite3", staged / migration.MEMORY)
            if console_path.exists():
                console["groups"] = {}
                migration.write_json(staged / "data/console.json", console)
            settings = root / "group_chat.json"
            if settings.exists():
                data = migration.read_json(settings)
                for item in data.get("groups", {}).values():
                    item.pop("persona", None)
                migration.write_json(staged / "group_chat.json", data)
            migration.write_json(staged / MARKER, {"version": 1, **counts})
            plan = {
                p.relative_to(staged).as_posix(): p
                for p in staged.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            }
            for bot_dir in (staged / "data/memory-vault").glob("*"):
                if bot_dir.is_dir() and bot_dir.name.isdigit():
                    subset = {
                        name: path
                        for name, path in plan.items()
                        if name.startswith(f"data/memory-vault/{bot_dir.name}/")
                    }
                    merge_artifacts(root, staged, subset, int(bot_dir.name))
                    plan.update(subset)
            paths = [
                *plan,
                "data/knowledge-cleanup.json",
                migration.MEMORY + "-wal",
                migration.MEMORY + "-shm",
            ]
            rollback = root / "backups" / ("memory-upgrade-" + uuid.uuid4().hex)
            for name in paths:
                target = migration.safe_path(root, name)
                if target.exists():
                    backup = migration.safe_path(rollback, name)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
            migration.write_json(
                root / migration.JOURNAL,
                {"rollback": rollback.relative_to(root).as_posix(), "paths": paths},
            )
            try:
                for name in paths:
                    target = migration.safe_path(root, name)
                    if name in plan:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(plan[name], target)
                    else:
                        target.unlink(missing_ok=True)
                (root / migration.JOURNAL).unlink()
            except BaseException:
                migration.recover(root)
                raise
            return {
                "state": "upgraded",
                "legacy_backup": rollback.relative_to(root).as_posix(),
                **counts,
            }


def validate_reference(ref, qq):
    if not isinstance(ref, dict) or set(ref) != {
        "bot",
        "grp",
        "usr",
        "message_id",
        "created",
        "hash",
    }:
        raise ValueError("记忆来源结构无效")
    if ref["bot"] != qq or any(
        type(ref[k]) is not int for k in ("bot", "grp", "usr", "message_id")
    ):
        raise ValueError("记忆来源账号无效")
    if not isinstance(ref["hash"], str) or not re.fullmatch("[a-f0-9]{64}", ref["hash"]):
        raise ValueError("记忆来源指纹无效")
    if type(ref["created"]) not in (int, float) or not math.isfinite(ref["created"]):
        raise ValueError("记忆来源时间无效")


def merge_artifacts(root, staged, plan, qq):
    """目标版本优先；不同来源版本隔离留存，完成任务不能回退。"""
    from plugins.assistant.vault import KINDS, digest

    for name, path in list(plan.items()):
        if not name.startswith(("data/memory-vault/", "data/memory-jobs/")):
            continue
        if name.split("/")[2] != str(qq):
            raise ValueError("记忆账号与迁移账号不一致")
        target = root / name
        if name.startswith("data/memory-vault/"):
            note = Vault(staged, qq).get(path.stem)
            if (
                note["kind"] not in KINDS
                or type(note.get("subject")) is not int
                or type(note.get("fixed")) is not bool
                or note.get("state") not in ("active", "archived", "quarantine")
            ):
                raise ValueError("记忆字段无效")
            finite_tree(note)
            if not isinstance(note.get("sources"), list) or not isinstance(
                note.get("history"), list
            ):
                raise ValueError("记忆来源和历史格式无效")
            if len(note["value"]) > 16000 or note["subject"] < 0:
                raise ValueError("记忆正文或主体无效")
            for key in ("weight", "reinforced", "created", "updated"):
                if type(note.get(key)) not in (int, float) or note[key] < 0:
                    raise ValueError("记忆权重或时间无效")
            for ref in note.get("sources", []):
                validate_reference(ref, qq)
            for old in note["history"]:
                if not isinstance(old, dict) or old.get("bot") != qq:
                    raise ValueError("记忆历史账号无效")
                for ref in old.get("sources", []):
                    validate_reference(ref, qq)
            if target.exists() and target.read_bytes() != path.read_bytes():
                # 不覆盖本机人工固定；冲突全文留在隔离笔记中等待维护者处理。
                conflict = dict(note)
                conflict["id"] = digest([note["id"], note["revision"]])[:32]
                conflict["state"] = "quarantine"
                conflict["import_conflict"] = note["id"]
                conflict.pop("revision", None)
                destination = Vault(staged, qq)
                previous = destination.get(conflict["id"])
                saved = destination.put(
                    conflict, previous["revision"] if previous else None, manual=True
                )
                extra = destination.target(saved["id"])
                plan[extra.relative_to(staged).as_posix()] = extra
                shutil.copy2(target, path)
        else:
            job = migration.read_json(path)
            if job.get("id") != path.stem or job.get("state") not in ("pending", "failed", "done"):
                raise ValueError("记忆队列状态无效")
            validate_job(job, qq)
            if not isinstance(job.get("completed"), list) or any(
                type(i) is not int or i < 0 for i in job["completed"]
            ):
                raise ValueError("记忆队列进度无效")
            if target.exists():
                local = migration.read_json(target)
                validate_job(local, qq)
                if local["source"] != job["source"]:
                    raise ValueError("记忆任务来源冲突")
                if (
                    local.get("actions") != job.get("actions")
                    and local.get("actions")
                    and job.get("actions")
                ):
                    # 不拼接不同模型计划中的索引；保留已执行的目标计划。
                    job = local
                elif local["state"] == "done":
                    job = local
                else:
                    job["completed"] = sorted(set(job["completed"]) | set(local["completed"]))
                    if local.get("actions"):
                        job["actions"] = local["actions"]
                        job["revisions"] = local.get("revisions", {})
                migration.write_json(path, job)


def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("迁移内容包含非有限数值")
    if isinstance(value, dict):
        for child in value.values():
            finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            finite_tree(child)


def validate_job(job, qq):
    from plugins.assistant.vault import digest

    finite_tree(job)
    validate_reference(job.get("source"), qq)
    if job.get("id") != digest(job["source"])[:32]:
        raise ValueError("记忆任务编号不匹配来源")
    if type(job.get("attempts")) is not int or job["attempts"] < 0:
        raise ValueError("记忆任务重试次数无效")
    if type(job.get("retry_at")) not in (int, float) or job["retry_at"] < 0:
        raise ValueError("记忆任务重试时间无效")
    actions = job.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 5:
        raise ValueError("记忆动作无效")
    revisions = job.get("revisions", {})
    if not isinstance(revisions, dict) or any(
        not re.fullmatch("[a-f0-9]{32}", k)
        or not isinstance(v, str)
        or not re.fullmatch("[a-f0-9]{64}", v)
        for k, v in revisions.items()
    ):
        raise ValueError("记忆版本无效")
    for item in actions:
        if (
            not isinstance(item, dict)
            or item.get("action") not in ("ADD", "UPDATE", "CORRECT", "REINFORCE")
            or item.get("kind") not in ("fact", "event")
        ):
            raise ValueError("记忆动作类型无效")
        for key, limit in (("field", 100), ("value", 6000), ("evidence", 6000)):
            if not isinstance(item.get(key), str) or not 1 <= len(item[key].strip()) <= limit:
                raise ValueError("记忆动作正文无效")
        if item["action"] != "ADD" and item.get("target") not in revisions:
            raise ValueError("记忆动作缺少目标版本")
        if item["action"] == "ADD" and item.get("target", ""):
            raise ValueError("新增记忆不允许指定目标")
        if not isinstance(item.get("occurred_at", "unknown"), str):
            raise ValueError("事件日期无效")
    completed = job.get("completed")
    if (
        not isinstance(completed, list)
        or len(set(completed)) != len(completed)
        or any(type(i) is not int or not 0 <= i < len(actions) for i in completed)
    ):
        raise ValueError("记忆执行进度无效")


def convert_staged_legacy(staged, plan):
    """导入暂存区中的旧资料先独立转换，再与新版笔记合并。"""
    with tempfile.TemporaryDirectory(prefix="qqbot-legacy-import-") as temporary:
        work = Path(temporary)
        for name in (
            migration.MEMORY,
            "data/console.json",
            "data/knowledge-cleanup.json",
            "group_chat.json",
        ):
            path = staged / name
            if path.exists():
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        if not required(work):
            return
        upgrade(work, apply=True)
        from plugins.assistant.vault import digest

        for path in (work / "data/memory-vault").glob("*/*.md"):
            name = path.relative_to(work).as_posix()
            target = staged / name
            if target.exists() and target.read_bytes() != path.read_bytes():
                bot = int(path.parent.name)
                note = Vault(work, bot).get(path.stem)
                note["id"] = digest(["legacy-import", note["id"], note["value"], note["sources"]])[
                    :32
                ]
                note["state"] = "quarantine"
                note.pop("revision", None)
                vault = Vault(staged, bot)
                old = vault.get(note["id"])
                vault.put(note, old["revision"] if old else None, manual=True)
                target = vault.target(note["id"])
                name = target.relative_to(staged).as_posix()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            plan[name] = target
        for name in (migration.MEMORY, "data/console.json", "group_chat.json", MARKER):
            path = work / name
            if path.exists():
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                plan[name] = target
