"""统一 Markdown 记忆库：来源校验、乐观并发、人工固定与历史保留。"""

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import yaml

KINDS = ("fact", "event", "impression", "thought", "diary", "self", "index", "context", "emote")
FIELDS = ("总体认知", "角色", "互动习惯", "互动关系", "补充认知", "会话印象")


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def weight(note):
    if note.get("fixed") or note["kind"] == "self":
        return 1
    age = max(0, time.time() - note["reinforced"])
    return max(0.01, note.get("weight", 1) * 2 ** (-age / 2592000))


def references(notes, budget=2400):
    """三路限额与正文上限，完整原文仍通过管理或记忆工具按需读取。"""
    counts = {"fact": 0, "personal": 0, "index": 0, "self": 0}
    limits = {"fact": (3, 1200), "personal": (2, 1600), "index": (1, 600), "self": (1, 1600)}
    result, seen = [], set()
    for note in notes:
        if budget <= 0 or note["id"] in seen or not note["value"] or weight(note) < 0.1:
            continue
        seen.add(note["id"])
        lane = (
            note["kind"]
            if note["kind"] in ("self", "index")
            else "fact"
            if note["kind"] in ("fact", "event", "context")
            else "personal"
        )
        maximum, remaining = limits[lane]
        if counts[lane] >= maximum or remaining <= 0:
            continue
        text = note["value"][: min(remaining, budget)]
        budget -= len(text)
        counts[lane] += 1
        limits[lane] = (maximum, remaining - len(text))
        result.append(
            {k: note[k] for k in ("id", "kind", "subject", "sources", "updated")}
            | {
                "value": text,
                "truncated": len(text) < len(note["value"]),
                "fixed": note.get("fixed", False),
                "field": note.get("field", ""),
            }
        )
    return result


def source(row):
    """使用平台标识与正文指纹，迁移后不依赖 SQLite 自增编号。"""
    return {k: row[k] for k in ("bot", "grp", "usr", "message_id", "created")} | {
        "hash": digest(row["content"])
    }


def identifier(subject, field, kind="impression"):
    return digest([kind, subject, field])[:32]


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        pending.write_text(text, encoding="utf-8", newline="\r\n")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


@lru_cache(maxsize=128)
def _parse(raw):
    """按实际文件内容缓存解析；每次读取仍检查磁盘，跨进程编辑立即生效。"""
    parts = raw.split("---\n", 2)
    if len(parts) != 3 or parts[0]:
        raise ValueError("记忆文件格式无效")
    note = yaml.load(parts[1], Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(note, dict):
        raise ValueError("记忆文件格式无效")
    note["value"] = parts[2].strip("\n")
    note["revision"] = hashlib.sha256(raw.encode()).hexdigest()
    return note


class Vault:
    def __init__(self, root, bot):
        if type(bot) is not int or bot <= 0:
            raise ValueError("机器人账号无效")
        self.root, self.bot = Path(root), bot
        self.path = self.root / "data" / "memory-vault" / str(bot)

    def target(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ValueError("记忆编号无效")
        path = self.path / (ident + ".md")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("记忆路径无效")
        return path

    def get(self, ident):
        path = self.target(ident)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        note = deepcopy(_parse(raw))
        if note.get("bot") != self.bot or note.get("id") != ident:
            raise ValueError("记忆归属无效")
        return note

    def valid(self, note, trail=(), db=None):
        if note.get("state") != "active" or note.get("bot") != self.bot:
            return False
        if note.get("fixed"):
            return True
        if note["id"] in trail:
            return False
        for ident, revision in note.get("dependencies", {}).items():
            dependency = self.get(ident)
            if (
                not dependency
                or dependency["revision"] != revision
                or not self.valid(dependency, (*trail, note["id"]), db=db)
            ):
                return False
        sources = note.get("sources", [])
        if not sources:
            return note["kind"] in ("self", "thought", "diary", "index") or (
                note["kind"] == "emote"
                and note.get("field") == "表情描述:" + str(note.get("image_digest", ""))
                and bool(re.fullmatch(r"[a-f0-9]{64}", str(note.get("image_digest", ""))))
            )
        path = self.root / "data/memory.sqlite3"
        if not path.exists():
            return False
        owned = db is None
        if owned:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for item in sources:
                row = db.execute(
                    "SELECT content,created FROM messages WHERE bot=? AND grp=? AND usr=? "
                    "AND message_id=?",
                    (self.bot, item["grp"], item["usr"], item["message_id"]),
                ).fetchone()
                if row is None or digest(row[0]) != item["hash"] or row[1] != item["created"]:
                    return False
            return True
        finally:
            if owned:
                db.close()

    def list(self, *, subject=None, kind=None, history=False):
        result = []
        database = self.root / "data/memory.sqlite3"
        db = (
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            if not history and database.exists()
            else None
        )
        try:
            for path in sorted(self.path.glob("*.md")):
                if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                    continue
                note = self.get(path.stem)
                if note is None:
                    continue
                if subject is not None and note.get("subject") != subject:
                    continue
                if kind is not None and note["kind"] != kind:
                    continue
                if history or self.valid(note, db=db):
                    result.append(note)
        finally:
            if db is not None:
                db.close()
        if not history:
            fixed = {(n["subject"], n.get("field")) for n in result if n.get("fixed")}
            result = [
                n for n in result if n.get("fixed") or (n["subject"], n.get("field")) not in fixed
            ]
        return result

    def put(self, note, expected=None, *, manual=False, activity_events=None):
        import migration

        with migration.project_lock(self.root, name=".memory.lock"):
            ident = note.get("id") or uuid.uuid4().hex
            old = self.get(ident)
            if (old or {}).get("revision") != expected:
                raise ValueError("记忆版本冲突，请重新读取")
            if old and old.get("fixed") and not manual:
                raise ValueError("人工固定内容不可自动修改")
            kind = note.get("kind", (old or {}).get("kind", "impression"))
            value = note.get("value")
            if kind not in KINDS or not isinstance(value, str) or len(value) > 16000:
                raise ValueError("记忆类型或内容无效")
            subject = note.get("subject", (old or {}).get("subject", 0))
            if type(subject) is not int or subject < 0:
                raise ValueError("人物账号无效")
            now = time.time()
            result = {
                **(old or {}),
                **note,
                "id": ident,
                "bot": self.bot,
                "kind": kind,
                "subject": subject,
                "updated": now,
                "value": value.strip(),
            }
            result.pop("revision", None)
            result.setdefault("created", now)
            result.setdefault("weight", 1.0)
            result.setdefault("reinforced", now)
            result.setdefault("state", "active")
            result.setdefault("sources", [])
            result.setdefault("fixed", False)
            result.setdefault("title", f"{subject} · {result.get('field', kind)}")
            result.setdefault("aliases", [result["title"]])
            result.setdefault("type", "note")
            result.setdefault("permalink", ident)
            result.setdefault("tags", ["qqbot-memory", kind])
            if (
                type(result["fixed"]) is not bool
                or type(result["weight"]) not in (int, float)
                or not math.isfinite(result["weight"])
                or not 0 <= result["weight"] <= 1
                or not isinstance(result["sources"], list)
            ):
                raise ValueError("记忆元数据无效")
            for ref in result["sources"]:
                if (
                    not isinstance(ref, dict)
                    or set(ref) != {"bot", "grp", "usr", "message_id", "created", "hash"}
                    or ref["bot"] != self.bot
                    or any(type(ref[k]) is not int for k in ("bot", "grp", "usr", "message_id"))
                    or not isinstance(ref["hash"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", ref["hash"])
                ):
                    raise ValueError("记忆依据无效")
            if result["state"] not in ("active", "archived", "quarantine"):
                raise ValueError("记忆状态无效")
            if not manual:
                result["fixed"] = False
                for fixed in self.list(subject=subject, history=True):
                    if (
                        fixed.get("fixed")
                        and fixed.get("state") != "archived"
                        and fixed.get("field") == result.get("field")
                    ):
                        raise ValueError("该字段已人工固定")
            history = list((old or {}).get("history", []))
            if old:
                history.append({k: v for k, v in old.items() if k != "history"})
            result["history"] = history
            metadata = {k: v for k, v in result.items() if k != "value"}
            # Markdown 是唯一正文；索引可以随时从该目录重建。
            text = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            atomic(self.target(ident), text + "---\n" + result["value"] + "\n")
            from .memory_activity import emit

            event_id = emit(
                self.root,
                self.bot,
                "memory_changed",
                notes=[ident],
                action="updated" if old else "added",
                manual=manual,
                internal=bool(result.get("profile_baseline")),
                category=kind,
                subject=subject,
                changes=[
                    {
                        "id": ident,
                        "title": result.get("field") or result["title"],
                        "kind": kind,
                        "subject": subject,
                        "before": old["value"] if old else None,
                        "after": result["value"],
                        "before_state": old.get("state") if old else None,
                        "after_state": result.get("state"),
                        "before_fixed": old.get("fixed") if old else None,
                        "after_fixed": result.get("fixed"),
                        "before_revision": old["revision"] if old else None,
                        "after_revision": self.get(ident)["revision"],
                        "sources": result.get("sources", []),
                    }
                ],
            )
            if activity_events is not None and event_id:
                activity_events.append(event_id)
            return self.get(ident)

    def field(self, subject, field, kind="impression"):
        return self.get(identifier(subject, field, kind))

    def set_field(
        self, subject, field, value, *, sources=(), manual=False, expected=None, kind="impression"
    ):
        return self.put(
            {
                "id": identifier(subject, field, kind),
                "subject": subject,
                "field": field,
                "kind": kind,
                "value": value,
                "sources": list(sources),
                "fixed": manual,
                **({"profile_baseline": None} if kind == "self" else {}),
            },
            expected,
            manual=manual,
        )

    def recall(self, query, subject=None, limit=6):
        terms = set(re.findall(r"[a-z0-9_]{2,}", query.lower()))
        for word in re.findall(r"[\u4e00-\u9fff]+", query):
            terms.update(word[i : i + 2] for i in range(len(word) - 1))
        ranked = []
        for note in self.list():
            priority = weight(note)
            if priority < 0.1 or not note["value"]:
                continue
            score = sum(t in note["value"].lower() for t in terms)
            score += 2 if subject and note["subject"] == subject else 0
            score += 3 if note["kind"] == "self" else 0
            if score:
                ranked.append((score * priority, note["updated"], note))
        ordered = [item[2] for item in sorted(ranked, key=lambda x: x[:2], reverse=True)]
        profiles = [n for n in ordered if n["kind"] == "self"]
        return (profiles[:1] + [n for n in ordered if n["kind"] != "self"])[:limit]

    def person(self, subject):
        notes = self.list(subject=subject)
        value = {
            field: "\n".join(n["value"] for n in notes if n.get("field") == field)
            for field in FIELDS
        }
        return value, notes

    def overview(self):
        """由当前有效证据生成阅读视图，不复制成另一份事实权威。"""
        groups = {}
        for note in sorted(self.list(), key=lambda n: n["updated"], reverse=True):
            if not note["value"].strip() or note["kind"] == "index":
                continue
            kind, subject = note["kind"], note["subject"]
            category = (
                "机器人"
                if kind in ("self", "thought", "diary")
                else "表情"
                if kind == "emote"
                else "群背景"
                if kind == "context"
                else "事件"
                if kind == "event"
                else "人物"
                if subject
                else "主题"
            )
            topic = note.get("field", kind)
            key = f"{category}:{subject}"
            if category in ("群背景", "主题", "表情"):
                key += ":" + (note.get("image_digest") or topic.split(":")[0])
            group = groups.setdefault(
                key,
                {
                    "key": key,
                    "category": category,
                    "subject": subject,
                    "title": f"{subject} 的档案"
                    if category == "人物"
                    else f"{subject} 的事件"
                    if category == "事件"
                    else "机器人档案"
                    if category == "机器人"
                    else topic,
                    "notes": [],
                    "content": "",
                },
            )
            group["notes"].append(note)
        for group in groups.values():
            sections, seen = [], set()
            for note in group["notes"]:
                normalized = re.sub(r"\s+", "", note["value"]).casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                label = note.get("field", note["kind"])
                if note["kind"] == "event" and note.get("occurred_at", "unknown") != "unknown":
                    label = note["occurred_at"] + " · " + label
                sections.append(f"### {label}\n{note['value']}")
            group["content"] = "\n\n".join(sections)
        return list(groups.values())
