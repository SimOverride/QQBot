"""定期生成可失效的认知整理视图，原始依据与人工固定内容保持不变。"""

import hashlib
import json
import sqlite3
import time
import uuid
from collections import defaultdict
from contextlib import closing

from .trace import emit

FILE = "data/knowledge-cleanup.json"
FIXED = ("后台维护者修正", "本人通过管理指令设置")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def read(root):
    try:
        value = json.loads((root / FILE).read_text(encoding="utf-8"))
        return (
            {k: v for k, v in value.items() if isinstance(v, dict)}
            if isinstance(value, dict)
            else {}
        )
    except (OSError, ValueError):
        return {}


def buckets(rows):
    result = defaultdict(list)
    for row in rows:
        row = dict(row)
        key = (row["bot"], row["grp"], row["usr"], row["field"].split(":", 1)[0])
        result[key].append(row)
    return {key: sorted(values, key=lambda r: r["field"]) for key, values in result.items()}


def label(key):
    return "person:" + ":".join(map(str, key))


def organized(rows, root):
    """只使用与当前可见依据完全一致的整理结果，防止跨会话泄漏。"""
    cache = read(root)
    output = []
    for key, values in buckets(rows).items():
        cached = cache.get(label(key), {})
        if (
            not any(r["evidence"].startswith(FIXED) for r in values)
            and cached.get("fingerprint") == fingerprint(values)
            and isinstance(cached.get("value"), str)
        ):
            output.append({**values[0], "value": cached["value"]})
        else:
            output.extend(values)
    return output


def group_source(db, bot, group, ceiling=None):
    rows = db.execute(
        "SELECT id,message_id,usr,content,created FROM messages "
        "WHERE bot=? AND grp=? AND usr<>? AND id<=? ORDER BY id DESC LIMIT 120",
        (bot, group, bot, ceiling if ceiling is not None else 9223372036854775807),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def automatic_group(root, bot, group):
    cached = read(root).get(f"group:{bot}:{group}", {})
    if not cached.get("source") or not isinstance(cached.get("value"), dict):
        return {}
    path = root / "data/memory.sqlite3"
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            source = group_source(db, bot, group, cached["source"][-1]["id"])
        return cached["value"] if fingerprint(source) == cached.get("fingerprint") else {}
    except (sqlite3.Error, KeyError):
        return {}


class Consolidation:
    def __init__(self, archive, root, config):
        self.archive, self.root, self.config = archive, root, config
        self.cache = read(root)

    def save(self):
        path = self.root / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def due(self, name, source, now):
        prior = self.cache.get(name, {})
        return (
            prior.get("fingerprint") != fingerprint(source)
            and now >= prior.get("retry_at", 0)
            and now - prior.get("updated", 0) >= self.config.knowledge_cleanup_interval_hours * 3600
        )

    async def tick(self, summarize):
        if not self.config.knowledge_cleanup_enabled:
            return
        from .prompt_store import read_console

        db, now = self.archive.db, time.time()
        work = []
        groups = buckets(db.execute("SELECT * FROM facts ORDER BY bot,grp,usr,field").fetchall())
        for key, source in groups.items():
            if any(r["evidence"].startswith(FIXED) for r in source) or not 2 <= len(source) <= 200:
                continue
            if self.due(label(key), source, now):
                work.append((label(key), "person", key, source))
        fixed = read_console(self.root).get("groups", {})
        for bot, group in db.execute("SELECT DISTINCT bot,grp FROM messages WHERE grp>0"):
            if f"{bot}:{group}" in fixed:
                continue
            source = group_source(db, bot, group)
            name = f"group:{bot}:{group}"
            if source and self.due(name, source, now):
                work.append((name, "group", (bot, group), source))
        # 按上次处理时间轮转，避免活跃个人长期挤占群整理机会。
        work.sort(key=lambda item: self.cache.get(item[0], {}).get("updated", 0))
        for name, kind, key, source in work[:4]:
            try:
                public_source = (
                    source
                    if kind == "person"
                    else [{**r, "content": r["content"][:600]} for r in source]
                )
                value = await summarize(kind, public_source)
                if kind == "person":
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"value"}
                        or not isinstance(value["value"], str)
                        or not 1 <= len(value["value"]) <= 12000
                    ):
                        raise ValueError("个人整理结果无效")
                    value = value["value"]
                    current = buckets(
                        db.execute(
                            "SELECT * FROM facts WHERE bot=? AND grp=? AND usr=?", key[:3]
                        ).fetchall()
                    ).get(key, [])
                else:
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"summary", "knowledge"}
                        or any(not isinstance(v, str) or len(v) > 6000 for v in value.values())
                    ):
                        raise ValueError("群整理结果无效")
                    if f"{key[0]}:{key[1]}" in read_console(self.root).get("groups", {}):
                        continue
                    current = group_source(db, *key, ceiling=source[-1]["id"])
                if fingerprint(current) != fingerprint(source):
                    continue
                previous = self.cache.get(name, {})
                history = list(previous.get("history", []))
                if "value" in previous and previous["value"] != value:
                    history.append(
                        {
                            "value": previous["value"],
                            "updated": previous.get("updated"),
                            "source": previous.get("source", []),
                        }
                    )
                self.cache[name] = {
                    "history": history,
                    "fingerprint": fingerprint(source),
                    "value": value,
                    "updated": now,
                    "source": source,
                }
                self.save()
                emit(
                    self.config,
                    "knowledge_cleanup",
                    scope=name,
                    result="saved",
                    sources=len(source),
                )
            except Exception as error:
                self.cache.setdefault(name, {})["retry_at"] = now + 3600
                self.save()
                emit(
                    self.config,
                    "knowledge_cleanup",
                    scope=name,
                    result="failed",
                    error_type=type(error).__name__,
                )
