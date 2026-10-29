"""本机记忆活动与可解释召回：记录标识和结果，不复制原始聊天正文。"""

import json
import time
import uuid
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from .vault import atomic, references, weight


def durable_activity(item):
    """有效变更单独保留，普通召回和无变化诊断不能挤占其容量。"""
    kind = item.get("kind")
    if kind not in ("memory_changed", "processing_completed", "emote_feedback"):
        return False
    if kind == "processing_completed" and item.get("result") != "saved":
        return False
    if item.get("category") in ("index", "diary") or item.get("internal"):
        return False
    changes = [c for c in item.get("changes", []) if isinstance(c, dict)]
    if changes and all(
        c.get("before") == c.get("after")
        and c.get("before_state") == c.get("after_state")
        and c.get("before_fixed") == c.get("after_fixed")
        for c in changes
    ):
        return False
    return bool(item.get("notes") or changes)


def emit(root, bot, kind, **details):
    import migration

    folder = Path(root) / "data/memory-activity" / str(bot)
    try:
        with migration.project_lock(Path(root), name=".memory-activity.lock"):
            ident = uuid.uuid4().hex
            item = {**details, "id": ident, "bot": bot, "kind": kind, "created": time.time()}
            atomic(folder / (ident + ".json"), json.dumps(item, ensure_ascii=False))
            paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            retained = {True: 0, False: 0}
            for path in paths:
                try:
                    stat = path.stat()
                    saved = _read_activity(
                        str(path.resolve()), stat.st_mtime_ns, stat.st_size, stat.st_ino
                    )
                    durable = isinstance(saved, dict) and durable_activity(saved)
                except (OSError, ValueError):
                    durable = False
                retained[durable] += 1
                if retained[durable] > 200:
                    path.unlink()
            return ident
    except (OSError, RuntimeError):
        # 诊断不可用不能回滚已经成功的正式记忆写入。
        pass


@lru_cache(maxsize=2048)
def _read_activity(path, modified, size, inode):
    """事件以原子替换发布；文件签名改变即重新读取，调用者拿到独立副本。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def recent(root, bot=None, limit=100):
    folder = Path(root) / "data/memory-activity"
    paths = (folder / str(bot)).glob("*.json") if bot else folder.glob("*/*.json")
    result = []
    for path in paths:
        try:
            stat = path.stat()
            item = _read_activity(str(path.resolve()), stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if isinstance(item, dict) and item.get("bot") == int(path.parent.name):
                result.append(deepcopy(item))
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda n: n.get("created", 0), reverse=True)[:limit]


def recall(vault, query, subject=None, indexed=(), budget=2400):
    candidates = vault.recall(query, subject, 30)
    seen = {n["id"] for n in candidates}
    effective = {n["id"]: n for n in vault.list()}
    for note in indexed:
        if note["id"] in effective and note["id"] not in seen:
            candidates.append(effective[note["id"]])
            seen.add(note["id"])
    selected = references(candidates, budget)
    chosen = {n["id"] for n in selected}
    excluded = []
    for note in vault.list(history=True):
        if note["id"] in chosen:
            continue
        reason = (
            "archived"
            if note.get("state") == "archived"
            else "quarantine"
            if note.get("state") == "quarantine"
            else "empty_protection"
            if not note["value"].strip()
            else "source_invalid"
            if not vault.valid(note)
            else "fixed_override"
            if note["id"] not in effective
            else "decayed"
            if weight(note) < 0.1
            else "budget"
            if note["id"] in seen
            else "not_relevant"
        )
        excluded.append({"id": note["id"], "reason": reason})
    return {
        "selected": selected,
        "excluded": excluded[:200],
        "candidate_count": len(candidates),
        "budget_chars": budget,
        "used_chars": sum(len(n["value"]) for n in selected),
        "meaning": "已提供给模型的参考，不代表模型必然采纳",
    }
