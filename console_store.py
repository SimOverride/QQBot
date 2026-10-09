"""本地后台资源读写：限定目标、乐观锁、修改记录与撤销。"""

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import migration
from plugins.assistant.knowledge import (
    KNOWLEDGE_LIMIT,
    display_name,
    person_value,
    read_directory,
    scope_name,
)
from plugins.assistant.prompt_store import catalog, read_console


def revision(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def atomic_json(path, value):
    """同目录临时文件替换，读者不会看到半个配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


class ConsoleStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.local = threading.local()

    def file_target(self, kind, name):
        """新增与删除只能操作资源目录中的普通文件。"""
        if kind not in ("persona", "style", "emote"):
            raise ValueError("只支持人格、风格和表情包")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or name != name.strip()
            or name.startswith(".")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        ):
            raise ValueError("名称须为 1 至 100 字，不能包含路径或特殊字符")
        folder = {
            "persona": self.root / "personas",
            "style": self.root / "styles",
            "emote": migration.emote_dir(self.root),
        }[kind]
        filename = name if kind == "emote" else name + ".txt"
        if kind == "emote" and Path(filename).suffix.lower() not in (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
        ):
            raise ValueError("仅支持 PNG、JPEG、GIF、WebP 图片")
        return migration.safe_path(self.root, (folder / filename).relative_to(self.root).as_posix())

    def create_file(self, kind, name, content):
        """新资源不覆盖同名文件，与迁移及其他后台写入互斥。"""
        path = self.file_target(kind, name)
        if kind != "emote":
            if not isinstance(content, str) or not 1 <= len(content.strip()) <= 4000:
                raise ValueError("人格与风格须为 1 至 4000 字")
            content = content.strip().encode("utf-8")
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check_file_mutation()
            if path.exists():
                raise ValueError("同名资源已存在，请更换名称")
            return self.publish_file(kind + ":" + name, path, content)

    def check_file_mutation(self):
        if (self.root / migration.JOURNAL).exists():
            raise ValueError("有未恢复的导入，暂不能修改资源")

    def publish_file(self, resource, path, content):
        ident = uuid.uuid4().hex
        audit = self.root / "backups/admin-edits" / (ident + ".json")
        record = {
            "id": ident,
            "resource": resource,
            "action": "create",
            "before": None,
            "after": "新增文件",
            "created": time.time(),
            "status": "pending",
        }
        atomic_json(audit, record)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 独占创建，不能覆盖确认之后出现的同名文件。
        with path.open("xb") as stream:
            stream.write(content)
        result = self.get(resource)
        record.update(status="saved", after=result["value"], revision=result["revision"])
        atomic_json(audit, record)
        return result

    def delete_file(self, resource, expected):
        kind, _, name = resource.partition(":")
        path = self.file_target(kind, name)
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check_file_mutation()
            current = self.get(resource)
            if current["revision"] != expected:
                raise ValueError("内容已被其他操作修改，请重新载入后再删除")
            if kind in ("persona", "style"):
                if name == "默认":
                    raise ValueError("默认人格和风格不能删除")
                settings_path = self.root / "group_chat.json"
                settings = migration.read_json(settings_path) if settings_path.exists() else {}
                if any(
                    g.get(kind, {}).get("name", "").casefold() == name.casefold()
                    for g in settings.get("groups", {}).values()
                ):
                    raise ValueError("仍有群正在使用此提示词，请先切换该群设定再删除")
            ident = uuid.uuid4().hex
            audit = self.root / "backups/admin-edits" / (ident + ".json")
            audit.parent.mkdir(parents=True, exist_ok=True)
            # 保留完整原文件；同图副本共享的表情认知不随单个文件删除。
            backup = audit.with_suffix(".bin")
            backup.write_bytes(path.read_bytes())
            record = {
                "id": ident,
                "resource": resource,
                "action": "delete",
                "before": current["value"],
                "after": None,
                "created": time.time(),
                "status": "pending",
                "digest": migration.digest(backup),
            }
            atomic_json(audit, record)
            path.unlink()
            record["status"] = "saved"
            atomic_json(audit, record)
            return {"message": "已删除，原文件已备份，可在操作记录中撤销"}

    @contextmanager
    def db(self):
        """后台不能自动创建一个假空数据库。"""
        active = getattr(self.local, "connection", None)
        if active is not None:
            yield active
            return
        path = self.root / migration.MEMORY
        if not path.exists():
            raise ValueError("还没有聊天数据库，请先运行机器人或导入备份")
        db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def resolve(self, resource):
        if not isinstance(resource, str) or len(resource) > 240:
            raise ValueError("资源标识无效")
        kind, _, key = resource.partition(":")
        if kind == "prompt":
            if key not in catalog():
                raise ValueError("未知提示词")
        elif kind in ("persona", "style", "emote"):
            if "/" in key or "\\" in key or not key:
                raise ValueError("文件名无效")
            folder = {
                "persona": self.root / "personas",
                "style": self.root / "styles",
                "emote": migration.emote_dir(self.root),
            }[kind]
            file = key if kind == "emote" else key + ".txt"
            path = migration.safe_path(self.root, (folder / file).relative_to(self.root).as_posix())
            if not path.is_file():
                raise ValueError("文件不存在")
            return kind, path
        elif kind in ("person", "group"):
            ids = tuple(int(n) for n in key.split(":"))
            if len(ids) != 2 or ids[0] <= 0 or ids[1] <= 0:
                raise ValueError("账号或会话无效")
            if kind == "group" and ids[1] < 0:
                raise ValueError("仅支持群会话")
            migration.account(self.root, ids[0])
            return kind, ids
        elif kind == "message":
            return kind, int(key)
        else:
            raise ValueError("不支持的资源")
        return kind, key

    def get(self, resource):
        kind, key = self.resolve(resource)
        extra = {}
        if kind == "prompt":
            entry = catalog()[key]
            value = read_console(self.root).get("prompts", {}).get(key, entry["text"])
            extra = {
                "title": entry["title"],
                "default": entry["text"],
                "placeholders": entry["placeholders"],
            }
        elif kind in ("persona", "style"):
            value = key.read_text(encoding="utf-8-sig").strip()
        elif kind == "group":
            data = read_console(self.root).get("groups", {}).get(":".join(map(str, key)), {})
            settings_file = self.root / "group_chat.json"
            settings = migration.read_json(settings_file) if settings_file.exists() else {}
            group = settings.get("groups", {}).get(str(key[1]), {})
            value = {
                "summary": data.get("summary", ""),
                "knowledge": data.get("knowledge", ""),
                "activity": group.get("activity", settings.get("default_activity", 50)),
                "interests": group.get("interests", []),
                "persona": group.get("persona", {}).get("name", "默认"),
                "style": group.get("style", {}).get("name", "默认"),
            }
        elif kind == "person":
            with self.db() as db:
                value, extra["evidence"] = person_value(db, *key)
                extra["title"] = (
                    f"{display_name(db, *key, read_directory(self.root))} · QQ {key[1]}"
                )
        elif kind == "message":
            with self.db() as db:
                row = db.execute("SELECT * FROM messages WHERE id=?", (key,)).fetchone()
            if row is None:
                raise ValueError("消息不存在")
            value = {"content": row["content"]}
            extra["metadata"] = {k: row[k] for k in ("bot", "grp", "usr", "message_id", "created")}
        else:
            content_hash = migration.digest(key)
            db_path = key.parent / ".memory.sqlite3"
            text = ""
            if db_path.exists():
                db = sqlite3.connect(db_path)
                try:
                    row = db.execute(
                        "SELECT description FROM emote_memory WHERE digest=?", (content_hash,)
                    ).fetchone()
                    text = row[0] if row else ""
                finally:
                    db.close()
            value = {"description": text}
            extra["digest"] = content_hash
        return {"resource": resource, "value": value, "revision": revision([value, extra]), **extra}

    def validate(self, resource, value):
        kind, key = self.resolve(resource)
        current = self.get(resource)["value"]
        if isinstance(current, dict):
            if not isinstance(value, dict) or set(value) != set(current):
                raise ValueError("必须保留当前资源的全部字段")
        elif not isinstance(value, str):
            raise ValueError("提示词必须是文本")
        if kind == "prompt":
            if not 1 <= len(value.strip()) <= 20000:
                raise ValueError("提示词须为 1 至 20000 字")
            for name in catalog()[key]["placeholders"]:
                if "{" + name + "}" not in value:
                    raise ValueError("不能删除动态占位符：" + name)
        elif kind in ("persona", "style"):
            if not 1 <= len(value.strip()) <= 4000:
                raise ValueError("人格与风格须为 1 至 4000 字")
        elif kind == "person":
            if (
                not isinstance(value["总体认知"], str)
                or len(value["总体认知"]) > max(KNOWLEDGE_LIMIT, len(current["总体认知"]))
                or not isinstance(value["会话印象"], dict)
                or set(value["会话印象"]) != set(current["会话印象"])
            ):
                raise ValueError("须保留一段总体认知和全部会话印象，总体认知最多12000字")
            if any(
                not isinstance(v, str) or len(v) > max(KNOWLEDGE_LIMIT, len(current["会话印象"][g]))
                for g, v in value["会话印象"].items()
            ):
                raise ValueError("每个会话印象须为文本，最多12000字")
        elif kind == "group":
            if any(
                not isinstance(value[f], str) or len(value[f]) > 6000
                for f in ("summary", "knowledge")
            ):
                raise ValueError("群摘要及认知各最多 6000 字")
            if type(value["activity"]) is not int or not 0 <= value["activity"] <= 100:
                raise ValueError("积极性须为 0 至 100 的整数")
            if (
                not isinstance(value["interests"], list)
                or len(value["interests"]) > 20
                or any(not isinstance(v, str) or len(v) > 100 for v in value["interests"])
            ):
                raise ValueError("兴趣最多 20 项，每项最多 100 字")
            for field in ("persona", "style"):
                self.resolve(field + ":" + value[field])
        elif kind == "emote":
            if (
                not isinstance(value["description"], str)
                or len(value["description"].strip()) > 1200
            ):
                raise ValueError("表情认知最多 1200 字；留空后可重新识别")
        elif not isinstance(value["content"], str) or not 1 <= len(value["content"]) <= 20000:
            raise ValueError("消息内容须为 1 至 20000 字")

    def put(self, resource, value, expected):
        """数据库资源的版本检查与修改共用写事务，避免后台提取抢写。"""
        if self.resolve(resource)[0] not in ("person", "message"):
            return self._put(resource, value, expected)
        with self.db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.local.connection = db
            try:
                return self._put(resource, value, expected)
            finally:
                del self.local.connection

    def _put(self, resource, value, expected):
        """与迁移互斥，持久化旧值后修改；不同版本不能覆盖。"""
        with migration.project_lock(self.root, name=".migration.lock"):
            if (self.root / migration.JOURNAL).exists():
                raise ValueError("有未恢复的导入，暂不能编辑")
            old = self.get(resource)
            if old["revision"] != expected:
                raise ValueError("内容已被其他操作修改，请重新载入后再保存")
            self.validate(resource, value)
            if self.resolve(resource)[0] in ("person", "group"):
                path = self.proposal_path(resource)
                proposal = migration.read_json(path) if path.exists() else None
                if proposal and proposal.get("value") == value and not self.proposal(resource):
                    raise ValueError("建议依据已变化，请重新载入并生成")
            if old["value"] == value:
                return old
            ident = uuid.uuid4().hex
            audit = self.root / "backups/admin-edits" / (ident + ".json")
            record = {
                "id": ident,
                "resource": resource,
                "before": old["value"],
                "after": value,
                "created": time.time(),
                "status": "pending",
            }
            if self.resolve(resource)[0] in ("person", "group"):
                record["before_evidence"] = old.get("evidence", [])
                proposal = self.proposal(resource)
                if proposal and proposal.get("value") == value:
                    record["basis"] = proposal.get("explanation", "")
                    record["history"] = proposal.get("history")
            atomic_json(audit, record)
            self._write(resource, value)
            result = self.get(resource)
            record.update(status="saved", revision=result["revision"])
            atomic_json(audit, record)
            return result

    def _write(self, resource, value):
        kind, key = self.resolve(resource)
        if kind in ("prompt", "group"):
            config = read_console(self.root)
            if kind == "prompt":
                config.setdefault("prompts", {})[key] = value
            else:
                config.setdefault("groups", {})[":".join(map(str, key))] = {
                    f: value[f] for f in ("summary", "knowledge")
                }
                settings_path = self.root / "group_chat.json"
                settings = migration.read_json(settings_path) if settings_path.exists() else {}
                group = settings.setdefault("groups", {}).setdefault(str(key[1]), {})
                group.update({f: value[f] for f in ("activity", "interests")})
                for field in ("persona", "style"):
                    if value[field] == "默认":
                        group.pop(field, None)
                    else:
                        group[field] = {"name": value[field]}
                atomic_json(settings_path, settings)
            atomic_json(self.root / "data/console.json", config)
        elif kind in ("persona", "style"):
            pending = key.with_suffix(".pending")
            pending.write_text(value, encoding="utf-8")
            pending.replace(key)
        elif kind in ("person", "message"):
            with self.db() as db, db:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                if kind == "person":
                    bot, user = key
                    # 只替换实际修改的段落，保留其他会话的来源与自动处理状态。
                    previous, _ = person_value(db, *key)
                    old_layers = {"0": previous["总体认知"], **previous["会话印象"]}
                    changed = set()
                    layers = {
                        "0": {"总体认知": value["总体认知"]},
                        **{g: {"会话印象": v} for g, v in value["会话印象"].items()},
                    }
                    for group, fields in layers.items():
                        for field, text in fields.items():
                            if old_layers.get(group, "") == text:
                                continue
                            changed.add(int(group))
                            db.execute(
                                "DELETE FROM facts WHERE bot=? AND grp=? AND usr=?",
                                (bot, int(group), user),
                            )
                            db.execute(
                                "INSERT INTO facts VALUES(?,?,?,?,?,?,NULL,?)",
                                (
                                    bot,
                                    int(group),
                                    user,
                                    field,
                                    text,
                                    "后台维护者修正（固定）",
                                    time.time(),
                                ),
                            )
                    for row in db.execute(
                        "SELECT grp,MAX(id) AS latest FROM messages WHERE bot=? AND usr=? "
                        "GROUP BY grp",
                        key,
                    ).fetchall():
                        if 0 not in changed and row["grp"] not in changed:
                            continue
                        db.execute(
                            "INSERT INTO profile_cursor VALUES(?,?,?,?) ON CONFLICT(bot,grp,usr) "
                            "DO UPDATE SET last_id=MAX(last_id,excluded.last_id)",
                            (bot, row["grp"], user, row["latest"]),
                        )
                else:
                    row = db.execute("SELECT * FROM messages WHERE id=?", (key,)).fetchone()
                    db.execute("UPDATE messages SET content=? WHERE id=?", (value["content"], key))
                    column = "answer" if row["usr"] == row["bot"] else "question"
                    metadata = json.loads(row["metadata"] or "{}")
                    msg = metadata.get("response_to") if column == "answer" else row["message_id"]
                    if isinstance(msg, dict):
                        msg = msg.get("message_id")
                    db.execute(
                        f"UPDATE turns SET {column}=? WHERE bot=? AND grp=? AND message_id=?",
                        (value["content"], row["bot"], row["grp"], msg),
                    )
                    # 原文变更后不沿用旧认知依据和公开性审核，后续重新核验。
                    db.execute("DELETE FROM facts WHERE source_id=?", (key,))
                    db.execute(
                        "INSERT INTO disclosure_reviews VALUES(?,0) "
                        "ON CONFLICT(source_id) DO UPDATE SET safe=0",
                        (key,),
                    )
                    # 推进游标，使尚在请求中的旧个人认知提取不能重新写回。
                    scope = (row["bot"], row["grp"], row["usr"])
                    latest = db.execute(
                        "SELECT COALESCE(MAX(id),0) FROM messages WHERE bot=? AND grp=? AND usr=?",
                        scope,
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO profile_cursor VALUES(?,?,?,?) ON CONFLICT(bot,grp,usr) "
                        "DO UPDATE SET last_id=MAX(last_id,excluded.last_id)",
                        (*scope, latest),
                    )
        else:
            db = sqlite3.connect(key.parent / ".memory.sqlite3")
            try:
                with db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS emote_memory(digest TEXT PRIMARY KEY, "
                        "description TEXT NOT NULL, updated REAL NOT NULL)"
                    )
                    db.execute(
                        "INSERT INTO emote_memory VALUES(?,?,?) ON CONFLICT(digest) "
                        "DO UPDATE SET description=excluded.description,updated=excluded.updated",
                        (migration.digest(key), value["description"], time.time()),
                    )
            finally:
                db.close()

    def changes(self):
        files = sorted(
            (self.root / "backups/admin-edits").glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:100]
        return [migration.read_json(p) for p in files]

    def undo(self, ident):
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("修改记录无效")
        record = migration.read_json(self.root / "backups/admin-edits" / (ident + ".json"))
        if record["status"] != "saved":
            raise ValueError("该记录未完成保存，请人工核对")
        if record.get("action") == "create":
            return self.delete_file(record["resource"], record["revision"])
        if record.get("action") == "delete":
            kind, _, name = record["resource"].partition(":")
            path = self.file_target(kind, name)
            with migration.project_lock(self.root, name=".migration.lock"):
                self.check_file_mutation()
                if path.exists():
                    raise ValueError("同名资源已存在，不能覆盖恢复")
                backup = self.root / "backups/admin-edits" / (ident + ".bin")
                if migration.digest(backup) != record["digest"]:
                    raise ValueError("原文件备份校验失败")
                return self.publish_file(record["resource"], path, backup.read_bytes())
        return self.put(record["resource"], record["before"], record["revision"])

    def inventory(self):
        people, groups = [], []
        directory = read_directory(self.root)
        if (self.root / migration.MEMORY).exists():
            with self.db() as db:
                for row in db.execute(
                    "SELECT p.bot,p.usr,COUNT(m.id) AS count,MAX(m.id) AS latest,"
                    "COUNT(DISTINCT m.grp) AS scopes FROM "
                    "(SELECT bot,usr FROM messages UNION SELECT bot,usr FROM facts) p "
                    "LEFT JOIN messages m ON m.bot=p.bot AND m.usr=p.usr "
                    "WHERE p.usr<>p.bot GROUP BY p.bot,p.usr ORDER BY latest DESC"
                ):
                    name = display_name(db, row["bot"], row["usr"], directory)
                    value, _ = person_value(db, row["bot"], row["usr"])
                    sessions = [
                        {"scope": int(g), "name": scope_name(db, row["bot"], int(g), directory)}
                        for g in value["会话印象"]
                    ]
                    member_groups = {
                        int(g)
                        for g, data in directory.get(str(row["bot"]), {}).get("groups", {}).items()
                        if row["usr"] in data.get("members", [])
                    }
                    member_groups.update(s["scope"] for s in sessions if s["scope"] > 0)
                    people.append(
                        {
                            **dict(row),
                            "name": name,
                            "sessions": sessions,
                            "groups": sorted(member_groups),
                            "resource": f"person:{row['bot']}:{row['usr']}",
                        }
                    )
                group_keys = {
                    (r["bot"], r["grp"])
                    for r in db.execute(
                        "SELECT bot,grp FROM messages WHERE grp>0 "
                        "UNION SELECT bot,grp FROM facts WHERE grp>0"
                    )
                }
                group_keys.update(
                    (int(bot), int(g))
                    for bot, data in directory.items()
                    for g in data.get("groups", {})
                )
                groups = [
                    {
                        "bot": bot,
                        "grp": group,
                        "name": scope_name(db, bot, group, directory),
                        "count": db.execute(
                            "SELECT COUNT(*) FROM messages WHERE bot=? AND grp=?", (bot, group)
                        ).fetchone()[0],
                    }
                    for bot, group in sorted(group_keys)
                ]
        return {
            "people": people,
            "groups": [{**g, "resource": f"group:{g['bot']}:{g['grp']}"} for g in groups],
            "prompts": [
                {"resource": "prompt:" + k, "name": v["title"]} for k, v in catalog().items()
            ],
            "personas": [
                {"resource": "persona:" + p.stem, "name": p.stem}
                for p in (self.root / "personas").glob("*.txt")
            ],
            "styles": [
                {"resource": "style:" + p.stem, "name": p.stem}
                for p in (self.root / "styles").glob("*.txt")
            ],
        }

    def knowledge_context(self, resource, cutoff=None):
        """按会话均衡抽取全时间段与近期消息，并说明实际覆盖范围。"""
        kind, key = self.resolve(resource)
        if kind not in ("person", "group"):
            return []
        bot, subject = key
        with self.db() as db:
            groups = (
                [subject]
                if kind == "group"
                else [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT grp FROM messages WHERE bot=? AND usr=? ORDER BY grp", key
                    )
                    if r[0] > 0 or r[0] == -subject
                ]
            )
            if len(groups) > 180:
                raise ValueError("会话过多，请先缩小历史资料范围后整理")
            limit = max(1, min(120, 180 // max(1, len(groups))))
            result = []
            for group in groups:
                clause = "bot=? AND grp=?"
                args = [bot, group]
                if kind == "person":
                    clause += " AND (usr=? OR related_usr=?)"
                    args.extend([subject, subject])
                if cutoff is not None:
                    clause += " AND id<=?"
                    args.append(cutoff)
                ids = [
                    r[0]
                    for r in db.execute(f"SELECT id FROM messages WHERE {clause} ORDER BY id", args)
                ]
                if not ids:
                    continue
                if len(ids) <= limit:
                    chosen = ids
                else:
                    recent = max(1, limit // 2)
                    older = limit - recent
                    chosen = [
                        ids[i * (len(ids) - recent) // max(1, older)] for i in range(older)
                    ] + ids[-recent:]
                messages = []
                for ident in chosen:
                    row = dict(db.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone())
                    metadata = json.loads(row["metadata"] or "{}")
                    messages.append(
                        {
                            "id": ident,
                            "qq": row["usr"],
                            "time": row["created"],
                            "text": row["content"][:600],
                            "truncated": len(row["content"]) > 600,
                            "name": metadata.get("sender_name"),
                            "role": metadata.get("sender_role_at_send"),
                            "mentions": metadata.get("mentions"),
                            "reply_to": metadata.get("reply_to"),
                            "response_to": metadata.get("response_to"),
                        }
                    )
                result.append(
                    {
                        "scope": group,
                        "total": len(ids),
                        "selected": len(messages),
                        "sampling": "全时间段均匀抽样与最近记录；不足上限时全量",
                        "messages": messages,
                    }
                )
            return result

    def proposal_path(self, resource):
        self.resolve(resource)
        return self.root / "backups/knowledge-proposals" / (revision(resource) + ".json")

    def proposal(self, resource):
        path = self.proposal_path(resource)
        if not path.exists():
            return None
        data = migration.read_json(path)
        history = data.get("history")
        if (
            data.get("revision") != self.get(resource)["revision"]
            or not history
            or history != self.history_version(resource, history["last_id"])
        ):
            return None
        return data

    def history_version(self, resource, cutoff=None):
        """只检查生成时已有的依据，新增聊天不使待确认建议失效。"""
        kind, key = self.resolve(resource)
        if not (self.root / migration.MEMORY).exists():
            return {"last_id": 0, "digest": revision([])}
        with self.db() as db:
            clause = "bot=? AND grp=?" if kind == "group" else "bot=? AND (usr=? OR related_usr=?)"
            args = key if kind == "group" else (*key, key[1])
            if cutoff is None:
                cutoff = db.execute(
                    f"SELECT COALESCE(MAX(id),0) FROM messages WHERE {clause}", args
                ).fetchone()[0]
            digest = hashlib.sha256()
            for row in db.execute(
                f"SELECT id,content,metadata FROM messages WHERE {clause} AND id<=? ORDER BY id",
                (*args, cutoff),
            ):
                digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
            return {"last_id": cutoff, "digest": digest.hexdigest()}

    def messages(self, group=None, user=None, query="", offset=0):
        clause, args = ["1=1"], []
        for column, value in (("grp", group), ("usr", user)):
            if value is not None:
                clause.append(column + "=?")
                args.append(value)
        if query:
            clause.append("instr(content,?)>0")
            args.append(query[:200])
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM messages WHERE "
                    + " AND ".join(clause)
                    + " ORDER BY created DESC,id DESC LIMIT 50 OFFSET ?",
                    [*args, offset],
                )
            ]

    def emotes(self):
        root = migration.emote_dir(self.root)
        return [
            {"name": p.name, "resource": "emote:" + p.name}
            for p in sorted(root.glob("*"))
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ]
