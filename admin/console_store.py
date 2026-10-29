"""本地后台资源读写：限定目标、乐观锁、修改记录与撤销。"""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import migration
from plugins.assistant.knowledge import (
    KNOWLEDGE_LIMIT,
    display_name,
    read_directory,
    scope_name,
)
from plugins.assistant.prompt_store import (
    migrate_console,
    module_defaults,
    module_value,
    modules,
    read_console,
    sparse_module,
    validate_module,
)
from plugins.assistant.topics import DEFAULT_MIN_PARTICIPANTS, validate_min_participants
from plugins.assistant.vault import FIELDS, KINDS, Vault, identifier


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
        migrate_console(self.root)

    def file_target(self, kind, name):
        """新增与删除只能操作资源目录中的普通文件。"""
        if kind not in ("persona", "emote"):
            raise ValueError("只支持人设和表情包")
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
            if not isinstance(content, str) or not 1 <= len(content.strip()) <= 12000:
                raise ValueError("人设须为 1 至 12000 字")
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

    def present(self, resource, value):
        """仅生成阅读副本；不用于修改认知或版本计算。"""
        from plugins.assistant.names import Names

        kind, key = self.resolve(resource)
        return Names(self.root, key[0]).data(value) if kind in ("person", "group") else value

    def delete_file(self, resource, expected):
        kind, _, name = resource.partition(":")
        path = self.file_target(kind, name)
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check_file_mutation()
            current = self.get(resource)
            if current["revision"] != expected:
                raise ValueError("内容已被其他操作修改，请重新载入后再删除")
            if kind == "persona":
                if name == "默认":
                    raise ValueError("默认人设不能删除")
                settings_path = self.root / "group_chat.json"
                settings = migration.read_json(settings_path) if settings_path.exists() else {}
                if settings.get("default_persona", "默认").casefold() == name.casefold():
                    raise ValueError("此人设是全局默认人设，请先切换默认人设")
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
        if kind == "module":
            if key not in modules():
                raise ValueError("未知能力模块")
        elif kind in ("persona", "emote"):
            if "/" in key or "\\" in key or not key:
                raise ValueError("文件名无效")
            folder = {
                "persona": self.root / "personas",
                "emote": migration.emote_dir(self.root),
            }[kind]
            file = key if kind == "emote" else key + ".txt"
            path = migration.safe_path(self.root, (folder / file).relative_to(self.root).as_posix())
            if not path.is_file():
                raise ValueError("文件不存在")
            return kind, path
        elif kind == "memory":
            bot, separator, ident = key.partition(":")
            if not separator or not bot.isdigit() or int(bot) <= 0:
                raise ValueError("记忆账号无效")
            migration.account(self.root, int(bot))
            vault = Vault(self.root, int(bot))
            if vault.get(ident) is None:
                raise ValueError("记忆不存在")
            return kind, (int(bot), ident)
        elif kind in ("person", "group"):
            ids = tuple(int(n) for n in key.split(":"))
            if len(ids) != 2 or ids[0] <= 0 or ids[1] <= 0:
                raise ValueError("账号或会话无效")
            if kind == "group" and ids[1] < 0:
                raise ValueError("仅支持群会话")
            migration.account(self.root, ids[0])
            return kind, ids
        else:
            raise ValueError("不支持的资源")
        return kind, key

    def get(self, resource):
        kind, key = self.resolve(resource)
        if kind in ("person", "group", "memory"):
            from memory_upgrade import required

            if required(self.root):
                raise ValueError("旧记忆尚未迁移，请先到数据迁移页面预演并执行统一记忆升级")
        extra = {}
        if kind == "module":
            entry = modules()[key]
            value = module_value(key, self.root)
            extra = {"title": entry["title"], "default": module_defaults(key), "metadata": entry}
        elif kind == "persona":
            value = key.read_text(encoding="utf-8-sig").strip()
        elif kind == "group":
            settings_file = self.root / "group_chat.json"
            from plugins.assistant.groupchat import GroupSettings

            settings = GroupSettings(settings_file).read()
            group = settings.get("groups", {}).get(str(key[1]), {})
            value = {"min_participants": group.get("min_participants")}
            extra["default_min_participants"] = settings.get(
                "default_min_participants", DEFAULT_MIN_PARTICIPANTS
            )
        elif kind == "person":
            with self.db() as db:
                value, extra["evidence"] = Vault(self.root, key[0]).person(key[1])
                extra["title"] = (
                    f"{display_name(db, *key, read_directory(self.root))} · QQ {key[1]}"
                )
        elif kind == "memory":
            note = Vault(self.root, key[0]).get(key[1])
            value = {"content": note["value"], "state": note["state"], "fixed": note["fixed"]}
            extra = {
                "title": note.get("field", note["kind"]),
                "kind": note["kind"],
                "subject": note["subject"],
                "evidence": note.get("sources", []),
                "history": note.get("history", []),
                "note_revision": note["revision"],
            }
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
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='emote_proposals'"
                    ).fetchone():
                        suggestion = db.execute(
                            "SELECT original,description FROM emote_proposals WHERE digest=?",
                            (content_hash,),
                        ).fetchone()
                        if suggestion and suggestion[0] == text:
                            extra["proposal"] = {
                                "value": {"description": suggestion[1]},
                                "reason": "重新查看原图后的描述修正，确认后保存",
                            }

                finally:
                    db.close()
            value = {"description": text}
            extra["digest"] = content_hash
            from plugins.assistant.emotes import Emotes

            library = Emotes(key.parent, project_root=self.root)
            try:
                bot = migration.account(self.root)
            except ValueError:
                bot = None
            note = library.description_note(bot, content_hash) if bot else None
            if note:
                value = {"description": note["value"]}
                extra.pop("proposal", None)
            extra["note_revision"] = note["revision"] if note else None
            extra["bot"] = bot
            extra["experiences"] = [
                {
                    **{k: v for k, v in n.items() if k != "history"},
                    "resource": f"memory:{bot}:{n['id']}",
                }
                for n in (Vault(self.root, bot).list(kind="emote") if bot else [])
                if n.get("image_digest") == content_hash
                and not n.get("field", "").startswith("表情描述")
            ]
        result = {
            "resource": resource,
            "value": value,
            "revision": revision([value, extra]),
            **extra,
        }
        if kind in ("person", "group"):
            from plugins.assistant.names import Names

            renderer = Names(self.root, key[0])
            result["display_value"] = renderer.data(value)
            result["display_parts"] = renderer.fragments(value)
            result["display_evidence"] = renderer.data(extra.get("evidence", []))
        return result

    def validate(self, resource, value):
        kind, key = self.resolve(resource)
        current = self.get(resource)["value"]
        if isinstance(current, dict):
            if not isinstance(value, dict) or set(value) != set(current):
                raise ValueError("必须保留当前资源的全部字段")
        elif not isinstance(value, str):
            raise ValueError("提示词必须是文本")
        if kind == "module":
            validate_module(key, value)
        elif kind == "persona":
            if not 1 <= len(value.strip()) <= 12000:
                raise ValueError("人设须为 1 至 12000 字")
        elif kind == "person":
            if set(value) != set(FIELDS) or any(
                not isinstance(v, str) or len(v) > max(KNOWLEDGE_LIMIT, len(current[k]))
                for k, v in value.items()
            ):
                raise ValueError("须保留统一个人档案全部字段，每项最多12000字")
        elif kind == "memory":
            if (
                not isinstance(value["content"], str)
                or len(value["content"]) > 16000
                or value["state"] not in ("active", "archived", "quarantine")
                or type(value["fixed"]) is not bool
            ):
                raise ValueError("记忆内容、状态或固定设置无效")
        elif kind == "group":
            if value["min_participants"] is not None:
                validate_min_participants(value["min_participants"])
        elif kind == "emote":
            if (
                not isinstance(value["description"], str)
                or len(value["description"].strip()) > 1200
            ):
                raise ValueError("表情认知最多 1200 字；留空后可重新识别")

    def put(self, resource, value, expected):
        """全部记忆写入与整理队列、数据迁移共用排他锁。"""
        return self._put(resource, value, expected)

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
            self._write(resource, value, old)
            result = self.get(resource)
            record.update(status="saved", revision=result["revision"])
            atomic_json(audit, record)
            return result

    def _write(self, resource, value, snapshot):
        kind, key = self.resolve(resource)
        if kind in ("module", "group"):
            config = read_console(self.root)
            if kind == "module":
                config.setdefault("modules", {})[key] = sparse_module(key, value)
            else:
                settings_path = self.root / "group_chat.json"
                settings = migration.read_json(settings_path) if settings_path.exists() else {}
                group = settings.setdefault("groups", {}).setdefault(str(key[1]), {})
                settings.pop("default_activity", None)
                for entry in settings["groups"].values():
                    entry.pop("activity", None)
                if value["min_participants"] is None:
                    group.pop("min_participants", None)
                else:
                    group["min_participants"] = value["min_participants"]
                group.pop("interests", None)
                group.pop("persona", None)
                atomic_json(settings_path, settings)
            atomic_json(self.root / "data/console.json", config)
        elif kind == "persona":
            pending = key.with_suffix(".pending")
            pending.write_text(value, encoding="utf-8")
            pending.replace(key)
        elif kind == "person":
            vault = Vault(self.root, key[0])
            previous = snapshot["value"]
            versions = {n["id"]: n["revision"] for n in snapshot["evidence"]}
            for field, content in value.items():
                if previous[field] == content:
                    continue
                vault.set_field(
                    key[1],
                    field,
                    content,
                    manual=True,
                    expected=versions.get(identifier(key[1], field)),
                )
        elif kind == "memory":
            vault = Vault(self.root, key[0])
            old = vault.get(key[1])
            vault.put(
                {
                    **old,
                    "value": value["content"],
                    "state": value["state"],
                    "fixed": True if value["content"] != old["value"] else value["fixed"],
                },
                snapshot["note_revision"],
                manual=True,
            )
        else:
            from plugins.assistant.emotes import Emotes

            library = Emotes(key.parent, project_root=self.root)
            image_digest = library.digest(key.name)
            bot = migration.account(self.root)
            note = library.description_data(image_digest, value["description"])
            Vault(self.root, bot).put(
                {**note, "fixed": True}, snapshot.get("note_revision"), manual=True
            )

    def change_page(self, offset=0, limit=20):
        """审计只解析当前页，旧版本正文由详情接口按需读取。"""
        files = sorted(
            (self.root / "backups/admin-edits").glob("*.json"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        offset, limit = max(0, offset), max(1, min(100, limit))
        records = [migration.read_json(p) for p in files[offset : offset + limit]]
        for record in records:
            record["retired"] = record.get("resource", "").startswith("prompt:")
        return {"items": records, "total": len(files), "has_more": offset + limit < len(files)}

    def changes(self):
        return self.change_page(limit=100)["items"]

    def undo(self, ident):
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("修改记录无效")
        record = migration.read_json(self.root / "backups/admin-edits" / (ident + ".json"))
        if record.get("resource", "").startswith("prompt:"):
            raise ValueError("旧提示词记录仅保留查阅，请在对应能力模块编辑并保存")
        if record["status"] != "saved":
            raise ValueError("该记录未完成保存，请人工核对")
        if record.get("action") == "memory_create":
            current = self.get(record["resource"])
            return self.put(
                record["resource"], {**current["value"], "state": "archived"}, record["revision"]
            )
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

    def navigation(self, section="base"):
        """页面导航只读取所需目录，不扫描记忆正文和个人统计。"""
        if section not in ("base", "groups", "prompts"):
            raise ValueError("导航分类无效")
        if section == "prompts":
            return {
                "modules": [
                    {"resource": "module:" + k, "name": v["title"]} for k, v in modules().items()
                ],
                "personas": [
                    {"resource": "persona:" + p.stem, "name": p.stem}
                    for p in (self.root / "personas").glob("*.txt")
                ],
            }
        directory = read_directory(self.root)
        bots = {
            int(p.name)
            for p in (self.root / "data/memory-vault").glob("*")
            if p.is_dir() and p.name.isdigit() and int(p.name) > 0
        }
        groups = {}
        if (self.root / migration.MEMORY).exists():
            with self.db() as db:
                if section == "base":
                    bots.update(r[0] for r in db.execute("SELECT DISTINCT bot FROM messages"))
                else:
                    for row in db.execute(
                        "SELECT bot,grp,COUNT(*) FROM messages WHERE grp>0 GROUP BY bot,grp"
                    ):
                        bot, group, count = row
                        groups[(bot, group)] = {
                            "bot": bot,
                            "grp": group,
                            "count": count,
                            "name": scope_name(db, bot, group, directory),
                            "resource": f"group:{bot}:{group}",
                        }
        if section == "groups":
            for bot, item in directory.items():
                for group, data in item.get("groups", {}).items():
                    groups.setdefault(
                        (int(bot), int(group)),
                        {
                            "bot": int(bot),
                            "grp": int(group),
                            "count": 0,
                            "name": data.get("name") or f"群名待同步（{group}）",
                            "resource": f"group:{bot}:{group}",
                        },
                    )
            return {"groups": [groups[key] for key in sorted(groups)]}
        from memory_upgrade import required

        return {"bots": sorted(bots), "memory_upgrade_required": required(self.root)}

    def inventory(self):
        people, groups, memories = [], [], []
        directory = read_directory(self.root)
        bots = {
            int(p.name)
            for p in (self.root / "data/memory-vault").glob("*")
            if p.is_dir() and p.name.isdigit() and int(p.name) > 0
        }
        if (self.root / migration.MEMORY).exists():
            with self.db() as db:
                bots.update(r[0] for r in db.execute("SELECT DISTINCT bot FROM messages"))
                keys = {(r[0], r[1]) for r in db.execute("SELECT DISTINCT bot,usr FROM messages")}
                for bot in bots:
                    keys.update(
                        (bot, n["subject"])
                        for n in Vault(self.root, bot).list()
                        if n["subject"] > 0
                    )
                for bot, user in sorted(keys):
                    if bot == user:
                        continue
                    rows = db.execute(
                        "SELECT grp,COUNT(*) AS count FROM messages WHERE bot=? "
                        "AND usr=? GROUP BY grp",
                        (bot, user),
                    ).fetchall()
                    people.append(
                        {
                            "bot": bot,
                            "usr": user,
                            "name": display_name(db, bot, user, directory),
                            "count": sum(r["count"] for r in rows),
                            "scopes": len(rows),
                            "groups": sorted(
                                {r["grp"] for r in rows if r["grp"] > 0}
                                | {
                                    int(group)
                                    for group, item in directory.get(str(bot), {})
                                    .get("groups", {})
                                    .items()
                                    if user in item.get("members", [])
                                }
                            ),
                            "resource": f"person:{bot}:{user}",
                        }
                    )
                keys = {
                    (r[0], r[1])
                    for r in db.execute("SELECT DISTINCT bot,grp FROM messages WHERE grp>0")
                }
                keys.update(
                    (int(bot), int(group))
                    for bot, item in directory.items()
                    for group in item.get("groups", {})
                )
                for bot, group in sorted(keys):
                    groups.append(
                        {
                            "bot": bot,
                            "grp": group,
                            "name": scope_name(db, bot, group, directory),
                            "count": db.execute(
                                "SELECT COUNT(*) FROM messages WHERE bot=? AND grp=?", (bot, group)
                            ).fetchone()[0],
                            "resource": f"group:{bot}:{group}",
                        }
                    )
        for bot in sorted(bots):
            for note in Vault(self.root, bot).list(history=True):
                memories.append(
                    {
                        "resource": f"memory:{bot}:{note['id']}",
                        "bot": bot,
                        "name": note.get("field", note["kind"]),
                        "kind": note["kind"],
                        "subject": note["subject"],
                        "state": note["state"],
                        "fixed": note["fixed"],
                        "sources": sorted({r["grp"] for r in note.get("sources", [])}),
                        "updated": note["updated"],
                        "empty": not bool(note["value"].strip()),
                        "preview": note["value"][:240],
                    }
                )
        from memory_upgrade import required

        return {
            "memory_upgrade_required": required(self.root),
            "people": people,
            "groups": groups,
            "memories": memories,
            "bots": sorted(bots),
            "modules": [
                {"resource": "module:" + k, "name": v["title"]} for k, v in modules().items()
            ],
            "personas": [
                {"resource": "persona:" + p.stem, "name": p.stem}
                for p in (self.root / "personas").glob("*.txt")
            ],
        }

    def create_memory(self, bot, kind, subject, field, content):
        """只允许维护者显式创建记忆，不让建议请求调用此入口。"""
        if (
            type(bot) is not int
            or type(subject) is not int
            or subject < 0
            or kind not in KINDS
            or not isinstance(field, str)
            or not 1 <= len(field) <= 100
            or not isinstance(content, str)
            or not 1 <= len(content) <= 16000
        ):
            raise ValueError("请填写有效的记忆归属、类别、名称和正文")
        migration.account(self.root, bot)
        from memory_upgrade import required

        if required(self.root):
            raise ValueError("请先完成统一记忆迁移，再创建记录")
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check_file_mutation()
            ident = uuid.uuid4().hex
            resource = f"memory:{bot}:{ident}"
            audit = self.root / "backups/admin-edits" / (uuid.uuid4().hex + ".json")
            record = {
                "id": audit.stem,
                "resource": resource,
                "before": None,
                "after": {"content": content, "fixed": True, "state": "active"},
                "created": time.time(),
                "status": "pending",
                "action": "memory_create",
            }
            atomic_json(audit, record)
            Vault(self.root, bot).put(
                {
                    "id": ident,
                    "kind": kind,
                    "subject": subject,
                    "field": field,
                    "value": content,
                    "fixed": True,
                },
                manual=True,
            )
            result = self.get(resource)
            record.update(status="saved", revision=result["revision"])
            atomic_json(audit, record)
            return result

    def memory_status(self):
        """队列只返回调度元数据，避免批量暴露原文和整理动作。"""
        jobs = []
        for path in sorted((self.root / "data/memory-jobs").glob("*/*.json")):
            try:
                job = migration.read_json(path)
                if not isinstance(job, dict) or "id" not in job or "state" not in job:
                    raise ValueError("任务格式无效")
            except (OSError, ValueError):
                job = {"id": path.stem, "state": "failed", "error": "InvalidJob"}
            jobs.append(
                {
                    "id": job["id"],
                    "bot": int(path.parent.name),
                    **{k: job.get(k) for k in ("state", "attempts", "retry_at", "error", "result")},
                }
            )
        indexes = []
        for path in sorted((self.root / "data/memory-index").glob("*/status.json")):
            data = migration.read_json(path)
            indexes.append(
                {
                    "bot": int(path.parent.name),
                    **{key: data.get(key) for key in ("state", "error", "updated", "mode")},
                }
            )
        issues = []
        for bot in self.navigation()["bots"]:
            vault = Vault(self.root, bot)
            for note in vault.list(history=True):
                if note["state"] == "quarantine" or (
                    note["state"] == "active" and not vault.valid(note)
                ):
                    issues.append(
                        {
                            "resource": f"memory:{bot}:{note['id']}",
                            "title": note.get("field", note["kind"]),
                            "reason": "待核对冲突"
                            if note["state"] == "quarantine"
                            else "来源已失效",
                        }
                    )
        return {
            "issues": issues[:100],
            "indexes": indexes,
            "counts": {
                state: sum(j["state"] == state for j in jobs)
                for state in ("pending", "failed", "done")
            },
            "jobs": [j for j in jobs if j["state"] != "done"][:100],
        }

    def memory_library(self, query="", category="", maintenance=False):
        """聚合视图只展示有效正文，底层来源与固定空值仍可在维护视图核对。"""
        inventory = self.navigation()
        directory = read_directory(self.root)
        names = {}
        if (self.root / migration.MEMORY).exists():
            with self.db() as db:
                for bot, user in db.execute("SELECT DISTINCT bot,usr FROM messages"):
                    names[(bot, user)] = display_name(db, bot, user, directory)
        groups = []
        query = query[:200].casefold()
        for bot in inventory["bots"]:
            vault = Vault(self.root, bot)
            for entry in vault.overview():
                if category and entry["category"] != category:
                    continue
                entry = dict(entry)
                entry["bot"] = bot
                if entry["category"] == "人物":
                    entry["title"] = names.get((bot, entry["subject"]), str(entry["subject"]))
                entry["notes"] = [
                    {
                        **{k: v for k, v in n.items() if k != "history"},
                        "resource": f"memory:{bot}:{n['id']}",
                    }
                    for n in entry["notes"]
                ]
                if query and query not in (entry["title"] + " " + entry["content"]).casefold():
                    continue
                groups.append(entry)
            if maintenance:
                for note in vault.list(history=True):
                    if note["value"].strip() and vault.valid(note) and note["kind"] != "index":
                        continue
                    if query and query not in (note.get("field", "") + note["value"]).casefold():
                        continue
                    groups.append(
                        {
                            "key": note["id"],
                            "bot": bot,
                            "category": "维护",
                            "subject": note["subject"],
                            "title": note.get("field", note["kind"]),
                            "content": note["value"] or "人工清空保护记录",
                            "notes": [{**note, "resource": f"memory:{bot}:{note['id']}"}],
                        }
                    )
        return {"groups": groups, "bots": inventory["bots"]}

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
        return {
            **data,
            "display_value": self.present(resource, data["value"]),
            "display_explanation": self.present(resource, data.get("explanation", "")),
        }

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

    def referenced_messages(self, resource, ids, scope=None):
        """只读查询当前认知会话中的来源消息，不按 QQ 消息号猜测匹配。"""
        kind, key = self.resolve(resource)
        if kind not in ("person", "group"):
            raise ValueError("请选择个人或群认知")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 100
            or any(type(n) is not int or not 0 < n < 2**63 for n in ids)
        ):
            raise ValueError("每次最多查询 100 个有效消息编号")
        with self.db() as db:
            scopes = (
                {key[1]}
                if kind == "group"
                else {
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT grp FROM messages WHERE bot=? AND usr=?", key
                    )
                }
            )
        if scope is not None:
            if type(scope) is not int or scope not in scopes:
                raise ValueError("消息不属于当前认知的会话")
            scopes = {scope}
        ids = list(dict.fromkeys(ids))
        result = []
        directory = read_directory(self.root)
        if scopes:
            with self.db() as db:
                rows = db.execute(
                    "SELECT * FROM messages WHERE bot=? AND grp IN ("
                    + ",".join("?" for _ in scopes)
                    + ") AND id IN ("
                    + ",".join("?" for _ in ids)
                    + ") ORDER BY created,id",
                    [key[0], *sorted(scopes), *ids],
                ).fetchall()
                for row in rows:
                    result.append(
                        {
                            "id": row["id"],
                            "sender": row["usr"],
                            "name": display_name(db, key[0], row["usr"], directory),
                            "scope": row["grp"],
                            "scope_name": scope_name(db, key[0], row["grp"], directory),
                            "is_self": row["usr"] == key[0],
                            "time": row["created"],
                            "text": row["content"],
                        }
                    )
        found = {r["id"] for r in result}
        return {"messages": result, "missing": [n for n in ids if n not in found]}

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
