"""定期生成可失效的认知整理视图，原始依据与人工固定内容保持不变。"""

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from contextlib import closing

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
            ids = [r["id"] for r in cached["source"]]
            source = [
                dict(row)
                for row in db.execute(
                    "SELECT id,message_id,usr,content,created FROM messages WHERE bot=? AND grp=? "
                    "AND id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id",
                    (bot, group, *ids),
                )
            ]
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
