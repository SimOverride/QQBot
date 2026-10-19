"""执行会话代际标记；重置不删除聊天档案或任务数据。"""

import uuid


def epoch(root, bot, scope):
    """核对全局与历史清理标记，跨进程读取原子发布的版本。"""
    folder = root / "data" / "agent-events"
    paths = (folder / "all.epoch", folder / str(bot) / f"{scope}.epoch")
    return tuple(path.read_text(encoding="utf-8") if path.exists() else "initial" for path in paths)


def reset(root):
    """发布新代际；已在执行的回合在下一次有效性检查时失效。"""
    folder = root / "data" / "agent-events"
    path = folder / "all.epoch"
    folder.mkdir(parents=True, exist_ok=True)
    value = uuid.uuid4().hex
    pending = path.with_name(path.name + "." + value + ".tmp")
    try:
        pending.write_text(value, encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return value
