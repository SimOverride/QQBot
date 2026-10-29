"""本地表情图片及统一记忆：视觉描述、使用反馈与成功发送账本。"""

import base64
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment

from .http import ServiceError
from .prompt_store import tool_definition

EMOTE_FUNCTIONS = [
    tool_definition("inspect_emote"),
    tool_definition("list_emotes"),
    tool_definition("remember_emotes"),
    tool_definition("choose_emote"),
]


class Emotes:
    def __init__(self, root: Path, max_bytes=5 * 1024 * 1024, daily_limit=20, project_root=None):
        self.root = root.resolve()
        self.project_root = Path(project_root).resolve() if project_root else self.root.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.daily_limit = daily_limit
        self.memory_path = self.root / ".memory.sqlite3"
        with closing(sqlite3.connect(self.memory_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS emote_memory ("
                "digest TEXT PRIMARY KEY, description TEXT NOT NULL, updated REAL NOT NULL)"
            )

    def setup_metadata(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS emote_usage (id TEXT PRIMARY KEY, "
            "bot INTEGER NOT NULL, scope INTEGER NOT NULL, digest TEXT NOT NULL, "
            "created REAL NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS emote_proposals (digest TEXT PRIMARY KEY, "
            "original TEXT NOT NULL, description TEXT NOT NULL, updated REAL NOT NULL)"
        )

    def record_sent(self, name, bot, scope, message_id=None, user=0, context=""):
        """仅成功发送后记录；事件编号使迁移重复导入保持幂等。"""
        from .vault import digest

        image_digest = self.digest(name)
        event_id = (
            digest([bot, scope, message_id, image_digest])
            if type(message_id) is int
            else uuid.uuid4().hex
        )
        with closing(sqlite3.connect(self.memory_path)) as db, db:
            self.setup_metadata(db)
            db.execute(
                "INSERT OR IGNORE INTO emote_usage VALUES(?,?,?,?,?)",
                (event_id, bot, scope, image_digest, time.time()),
            )

        from .emote_feedback import sent

        sent(self, bot, scope, self.digest(name), message_id, user, context)

    def usage(self, bot, scope):
        with closing(sqlite3.connect(self.memory_path)) as db:
            self.setup_metadata(db)
            return {
                r[0]: {"use_count": r[1], "last_used": r[2]}
                for r in db.execute(
                    "SELECT digest,COUNT(*),MAX(created) FROM emote_usage "
                    "WHERE bot=? AND scope=? GROUP BY digest",
                    (bot, scope),
                )
            }

    @staticmethod
    def score(query, text):
        """组合关键词与常见情绪同义词匹配。"""
        groups = (
            "无语 无奈 汗 失语",
            "开心 高兴 快乐 大笑",
            "赞同 同意 支持 点头",
            "晚安 睡觉 告别 再见",
            "难过 伤心 哭 委屈",
            "惊讶 震惊 吃惊",
        )
        score = 0
        for term in re.findall(r"[\w]+", query.casefold()):
            choices = {term}
            for group in groups:
                if term in group.split():
                    choices.update(group.split())
            score += max(
                (4 if w == term else 2 for w in choices if w in text.casefold()), default=0
            )
        return score

    def import_image(self, data, suffix, description, source, bot=0):
        from .emote_collect import image_data

        actual_suffix, _ = image_data(data)
        if suffix != actual_suffix or not 0 < len(data) <= self.max_bytes:
            raise ServiceError("收藏图片格式或大小不符合要求。")
        digest = hashlib.sha256(data).hexdigest()
        with closing(sqlite3.connect(self.memory_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS emote_imports ("
                "digest TEXT PRIMARY KEY, source TEXT NOT NULL, created REAL NOT NULL)"
            )
            db.execute("BEGIN IMMEDIATE")
            existing = None
            for name in self.files():
                try:
                    if self.digest(name) == digest:
                        existing = name
                        break
                except ServiceError:
                    continue
            if existing:
                if not bot:
                    db.execute(
                        "INSERT OR IGNORE INTO emote_memory VALUES(?,?,?)",
                        (digest, description, time.time()),
                    )
                self.import_description(bot, digest, description)
                return {"status": "already_saved", "id": existing, "digest": digest}
            count = db.execute(
                "SELECT COUNT(*) FROM emote_imports WHERE created>?", (time.time() - 86400,)
            ).fetchone()[0]
            if count >= self.daily_limit or len(self.files()) >= 1000:
                return {"status": "library_limit"}
            name = "收藏_" + digest + suffix
            path = self.root / name
            try:
                with path.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise ServiceError("收藏文件冲突，未覆盖。") from None
            db.execute(
                "INSERT INTO emote_imports VALUES(?,?,?)", (digest, source[:1000], time.time())
            )
            if not bot:
                db.execute(
                    "INSERT OR IGNORE INTO emote_memory VALUES(?,?,?)",
                    (digest, description, time.time()),
                )
        self.import_description(bot, digest, description)
        return {"status": "saved", "id": name, "digest": digest}

    def import_description(self, bot, image_digest, description):
        if bot:
            from .vault import Vault

            vault = Vault(self.project_root, bot)
            data = self.description_data(image_digest, description)
            if vault.get(data["id"]) is None:
                try:
                    vault.put(data)
                except ValueError:
                    # 其它会话已完成首次建立时沿用新版本，不能用旧描述覆盖。
                    if vault.get(data["id"]) is None:
                        raise

    def digest(self, name):
        encoded = self.message(name).data["file"].removeprefix("base64://")
        return hashlib.sha256(base64.b64decode(encoded)).hexdigest()

    def recall(self, digest, bot=0):
        if bot:
            note = self.description_note(bot, digest, migrate=True)
            return note["value"] if note and note.get("state") == "active" else None
        with closing(sqlite3.connect(self.memory_path)) as db:
            row = db.execute(
                "SELECT description FROM emote_memory WHERE digest=?", (digest,)
            ).fetchone()
        return row[0] if row else None

    def remember(self, items, expected, descriptions=None, bot=0):
        if not isinstance(items, list) or not items:
            raise ServiceError("表情记忆参数无效。")
        writes, seen = [], set()
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "description"}
                or not isinstance(item["id"], str)
                or item["id"] in seen
                or not isinstance(item["description"], str)
                or not 1 <= len(item["description"].strip()) <= 1200
            ):
                raise ServiceError("表情记忆参数无效。")
            name = item["id"]
            digest = self.digest(name)
            if expected.get(name) != digest:
                raise ServiceError("表情图片已变化或本次未查看，请重新检索。")
            seen.add(name)
            writes.append((digest, item["description"].strip(), time.time()))
        if bot:
            from .vault import Vault

            vault = Vault(self.project_root, bot)
            for image_digest, description, _ in writes:
                old = self.description_note(bot, image_digest, migrate=True)
                if descriptions is not None and image_digest in descriptions:
                    if (old["value"] if old else None) != descriptions[image_digest]:
                        raise ServiceError("表情描述已被修改，请重新查看")
                if old and old["value"] == description:
                    continue
                if old and old.get("fixed"):
                    raise ServiceError("人工固定的表情描述不能自动修改")
                vault.put(
                    self.description_data(image_digest, description),
                    old["revision"] if old else None,
                )
            return seen
        with closing(sqlite3.connect(self.memory_path)) as db, db:
            self.setup_metadata(db)
            for digest, description, stamp in writes:
                old = db.execute(
                    "SELECT description FROM emote_memory WHERE digest=?", (digest,)
                ).fetchone()
                if descriptions is not None and digest in descriptions:
                    if (old[0] if old else None) != descriptions[digest]:
                        raise ServiceError("表情描述已被修改，请重新查看后再提出建议")
                if old:
                    if old[0] != description:
                        db.execute(
                            "INSERT INTO emote_proposals VALUES(?,?,?,?) ON CONFLICT(digest) "
                            "DO UPDATE SET original=excluded.original,"
                            "description=excluded.description,updated=excluded.updated",
                            (digest, old[0], description, stamp),
                        )
                else:
                    db.execute(
                        "INSERT INTO emote_memory VALUES(?,?,?)", (digest, description, stamp)
                    )
        return seen

    @staticmethod
    def description_data(image_digest, value):
        from .vault import identifier

        return {
            "id": identifier(0, "表情描述:" + image_digest, "emote"),
            "kind": "emote",
            "subject": 0,
            "field": "表情描述:" + image_digest,
            "image_digest": image_digest,
            "title": "表情 · " + value[:32],
            "value": value,
            "sources": [],
        }

    def description_note(self, bot, image_digest, archive=None, migrate=False):
        """只读默认不迁移；正式使用时将旧描述一次性复制到对应机器人的统一库。"""
        from .vault import Vault

        vault = Vault(archive.root if archive else self.project_root, bot)
        data = self.description_data(image_digest, "")
        note = vault.get(data["id"])
        if note or not migrate:
            return note
        description = self.recall(image_digest)
        if description is None:
            return None
        try:
            # 旧库无法区分人工修正与自动识图，迁移时保守保护已有描述。
            return vault.put(
                {**data, "value": description, "legacy_import": True, "fixed": True},
                manual=True,
            )
        except ValueError:
            concurrent = vault.get(data["id"])
            if concurrent is None:
                raise
            return concurrent

    def experience(self, bot, image_digest, user=0, archive=None):
        """反馈跨会话共享；个人停用只约束本人，不扩散为其他人的禁用。"""
        from .vault import Vault

        vault = Vault(archive.root if archive else self.project_root, bot)
        notes = [
            n
            for n in vault.list(kind="emote")
            if n.get("image_digest") == image_digest
            and not n.get("field", "").startswith("表情描述:")
        ]
        notes.sort(key=lambda n: (n["subject"] == user, n["updated"]), reverse=True)
        applicable = [n for n in notes if n["subject"] in (0, user)]
        score = sum(
            2
            if n.get("sentiment") == "positive"
            else -3
            if n.get("sentiment") in ("negative", "avoid")
            else 0
            for n in applicable
        )
        return {
            "disabled": any(n.get("disabled") for n in applicable),
            "score": max(-6, min(4, score)),
            "notes": [
                {k: n.get(k) for k in ("id", "subject", "value", "sentiment", "sources", "updated")}
                for n in notes[:8]
            ],
        }

    def files(self):
        result = {}
        for path in sorted(self.root.iterdir()):
            if len(result) >= 1000:
                break
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                continue
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.resolve().parent != self.root
                    or not 0 < path.stat().st_size <= self.max_bytes
                ):
                    continue
            except OSError:
                continue
            result[path.name] = path
        return result

    def list(self, args, bot=0, scope=0, archive=None, user=0):
        if (
            not isinstance(args, dict)
            or set(args) != {"query", "offset"}
            or not isinstance(args["query"], str)
            or len(args["query"]) > 100
            or type(args["offset"]) is not int
            or not 0 <= args["offset"] <= 1000
        ):
            return {"status": "invalid_arguments"}
        known, unknown = [], []
        usage = self.usage(bot, scope)
        query = args["query"].casefold()
        for name in self.files():
            try:
                digest = self.digest(name)
            except ServiceError:
                continue
            state = self.experience(bot, digest, user, archive) if bot else {}
            if state.get("disabled"):
                continue
            description = self.recall(digest, bot)
            item = {
                "experience": state.get("notes", []),
                "feedback_score": state.get("score", 0),
                "id": name,
                "digest": digest,
                "description": description or "尚未识别",
                "memory_status": "known" if description else "unknown",
                **usage.get(digest, {"use_count": 0, "last_used": 0}),
                "score": self.score(query, name + " " + (description or "")),
            }
            if not query or item["score"] > 0:
                known.append(item)
            elif description is None:
                unknown.append(item)
        known.sort(
            key=lambda x: (
                -(x["score"] + x["feedback_score"]),
                x["last_used"] > time.time() - 600,
                x["use_count"],
                x["id"],
            )
        )
        rows = known or unknown
        start = args["offset"]
        return {
            "status": "ok" if rows[start : start + 3] else "no_matches",
            "emotes": rows[start : start + 3],
            "browsing_unknown": bool(not known and unknown),
            "next_offset": start + 3 if start + 3 < len(rows) else None,
        }

    def message(self, name):
        path = self.files().get(name)
        if path is None:
            raise ServiceError("表情包不存在、过大或不可读取，请检查本地表情包目录。")
        try:
            with path.open("rb") as stream:
                data = stream.read(self.max_bytes + 1)
        except OSError:
            raise ServiceError("表情包读取失败，请检查本地文件。") from None
        valid = (
            data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith(b"\xff\xd8\xff")
            or data.startswith((b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        )
        if not valid or len(data) > self.max_bytes:
            raise ServiceError("表情包格式无效或文件过大。")
        # 图片以 Base64 交给平台，发送前已核验文件。
        return MessageSegment.image("base64://" + base64.b64encode(data).decode("ascii"))

    def preview(self, name):
        encoded = self.message(name).data["file"].removeprefix("base64://")
        head = base64.b64decode(encoded[:32])
        mime = (
            "image/png"
            if head.startswith(b"\x89PNG")
            else "image/jpeg"
            if head.startswith(b"\xff\xd8")
            else "image/gif"
            if head.startswith(b"GIF")
            else "image/webp"
        )
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
