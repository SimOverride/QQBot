"""本地后台资源读写：限定目标、乐观锁、修改记录与撤销。"""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import migration
from plugins.assistant.longterm import FACT_FIELDS
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
            if len(ids) != (3 if kind == "person" else 2) or ids[0] <= 0 or ids[1] == 0:
                raise ValueError("账号或会话无效")
            if kind == "person" and (ids[2] <= 0 or (ids[1] < 0 and ids[1] != -ids[2])):
                raise ValueError("个人会话无效")
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
                rows = db.execute(
                    "SELECT * FROM facts WHERE bot=? AND grp=? AND usr=?", key
                ).fetchall()
            value = {field: "" for field in sorted(FACT_FIELDS)}
            value.update({r["field"]: r["value"] for r in rows})
            extra["evidence"] = [dict(r) for r in rows]
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
            if any(not isinstance(v, str) or len(v) > 160 for v in value.values()):
                raise ValueError("个人认知每项最多 160 字；留空表示清除")
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
                    for field, text in value.items():
                        # 空值也保存修正标记，避免旧消息或模型把清除的字段重新写回。
                        db.execute(
                            "INSERT INTO facts VALUES(?,?,?,?,?,?,NULL,?) "
                            "ON CONFLICT(bot,grp,usr,field) DO UPDATE SET "
                            "value=excluded.value,evidence=excluded.evidence,source_id=NULL,updated=excluded.updated",
                            (*key, field, text, "后台维护者修正（固定）", time.time()),
                        )
                    latest = db.execute(
                        "SELECT COALESCE(MAX(id),0) FROM messages WHERE bot=? AND grp=? AND usr=?",
                        key,
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO profile_cursor VALUES(?,?,?,?) ON CONFLICT(bot,grp,usr) "
                        "DO UPDATE SET last_id=MAX(last_id,excluded.last_id)",
                        (*key, latest),
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
        return self.put(record["resource"], record["before"], record["revision"])

    def inventory(self):
        people, groups = [], []
        if (self.root / migration.MEMORY).exists():
            with self.db() as db:
                for row in db.execute(
                    "SELECT bot,grp,usr,COUNT(*) AS count,MAX(id) AS latest "
                    "FROM messages WHERE usr<>bot "
                    "GROUP BY bot,grp,usr ORDER BY latest DESC"
                ):
                    metadata = db.execute(
                        "SELECT metadata FROM messages WHERE id=?", (row["latest"],)
                    ).fetchone()[0]
                    name = json.loads(metadata or "{}").get("sender_name") or str(row["usr"])
                    people.append(
                        {
                            **dict(row),
                            "name": name,
                            "resource": f"person:{row['bot']}:{row['grp']}:{row['usr']}",
                        }
                    )
                groups = [
                    dict(r)
                    for r in db.execute(
                        "SELECT bot,grp,COUNT(*) AS count FROM messages "
                        "WHERE grp>0 GROUP BY bot,grp"
                    )
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
