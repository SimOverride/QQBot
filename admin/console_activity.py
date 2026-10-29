"""记忆活动阅读视图：分页先筛选，展开时才读取历史详情。"""

import base64
import json
import re

from plugins.assistant.knowledge import read_directory
from plugins.assistant.memory_activity import durable_activity, recent


def _key(item):
    return (item.get("created", 0), item["id"])


def _changes(item):
    return [c for c in item.get("changes", []) if isinstance(c, dict)]


def _visible(item):
    return item.get("kind") == "processing_failed" or durable_activity(item)


def _events(root):
    # 生产端每个账号有容量上限；这里不扫描记忆库和任务目录。
    events = recent(root, limit=None)
    used = set()
    result = []
    for event in events:
        if event["id"] in used or not _visible(event):
            continue
        event = dict(event)
        if event.get("kind") in ("processing_completed", "emote_feedback"):
            attached = []
            matched = []
            for ident in event.get("change_event_ids", []):
                match = next(
                    (
                        e
                        for e in events
                        if e["id"] not in used
                        and e.get("kind") == "memory_changed"
                        and not e.get("manual")
                        and e.get("bot") == event.get("bot")
                        and ident == e["id"]
                    ),
                    None,
                )
                if match:
                    used.add(match["id"])
                    matched.append(match)
                    if _visible(match):
                        attached.extend(_changes(match))
            if attached:
                event["changes"] = attached
            if (
                matched
                and len(matched) == len(event.get("notes", []))
                and not any(_visible(e) for e in matched)
            ):
                continue
        result.append(event)
    return sorted(result, key=_key, reverse=True)


def _summary(item, directory):
    changes = _changes(item)
    first = changes[0] if changes else {}
    subject = first.get("subject", item.get("subject"))
    person = directory.get(str(item["bot"]), {}).get("users", {}).get(str(subject), {})
    name = person.get("name") or (f"QQ {subject}" if subject else "")
    topic = first.get("title") or first.get("field") or "记忆"
    if topic.startswith("表情反馈:"):
        topic = "表情使用偏好"
    kind = item.get("kind")
    if kind == "processing_failed":
        title = "记忆整理失败，需要检查"
        summary = "这次整理未能完成，可展开查看原因。"
    else:
        action = "新增" if first.get("before") is None and changes else "更新"
        title = f"{action}了{name + '的' if name else ''}{topic}"
        if kind == "emote_feedback":
            title = f"根据反馈更新了{name + '的' if name else ''}表情使用偏好"
        if len(changes) > 1:
            title += f"等 {len(changes)} 项记忆"
        summary = str(first.get("after") or "")[:180]
        if changes and topic in ("总体认知", "记忆", "兴趣", "偏好"):
            excerpt = " ".join(str(first.get("after") or "已清除原内容").split())[:42]
            title = f"{action}了{name + '的' if name else ''}{topic}：{excerpt}"
        state_action = _state_action(first)
        if state_action:
            title = f"{state_action}了{name + '的' if name else ''}{topic}"
            if len(changes) > 1:
                title += f"等 {len(changes)} 项记忆"
        if not changes:
            title = f"旧记录：更新了 {len(item.get('notes', []))} 条记忆（未记录摘要）"
            summary = "旧记录未保存变更快照，展开可查看关联记忆。"
    return {
        "id": item["id"],
        "bot": item["bot"],
        "title": title,
        "summary": summary,
        "created": item["created"],
        "origin": "人工修改" if item.get("manual") else "自动整理",
        "category": first.get("kind", item.get("category", "")),
        "change_count": len(changes) or len(item.get("notes", [])),
    }


def _state_action(change):
    """状态与人工保护改变同样属于用户需要看到的有效变化。"""
    if change.get("before_state") is not None and change.get("before_state") != change.get(
        "after_state"
    ):
        return {"archived": "归档", "active": "恢复", "quarantine": "隔离"}.get(
            change.get("after_state"), ""
        )
    if change.get("before_fixed") is not None and change.get("before_fixed") != change.get(
        "after_fixed"
    ):
        return "固定" if change.get("after_fixed") else "解除固定"
    return ""


def activity_page(root, cursor="", limit=20):
    """游标以时间与标识排序，新增活动不会令后续页重复。"""
    limit = max(1, min(int(limit), 100))
    items = _events(root)
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError
            boundary = (float(value[0]), str(value[1]))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("活动分页位置无效，请刷新列表") from exc
        items = [item for item in items if _key(item) < boundary]
    page = items[:limit]
    more = len(items) > limit
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps(_key(page[-1])).encode()).decode() if more else ""
    )
    directory = read_directory(root)
    return {
        "items": [_summary(item, directory) for item in page],
        "next_cursor": next_cursor,
        "has_more": more,
    }


def activity_detail(root, bot, ident):
    """只返回事件当时保存的值；旧记录不拿当前正文冒充历史。"""
    if not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise ValueError("活动标识无效")
    item = next((e for e in _events(root) if e["id"] == ident and e["bot"] == int(bot)), None)
    if item is None:
        raise ValueError("活动不存在或已过保留期限")
    result = _summary(item, read_directory(root))
    changes = []
    snapshots = _changes(item)
    for change in snapshots or [{"id": i} for i in item.get("notes", [])]:
        note_id = change.get("id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", note_id):
            continue
        historical = "after" in change
        sources = change.get("sources", []) if historical else []
        evidence = [
            f"{'私聊' if s.get('grp', 0) < 0 else '群 ' + str(s.get('grp', ''))} · "
            f"QQ {s.get('usr', '')} · 消息 {s.get('message_id', '')}"
            for s in sources
        ]
        changes.append(
            {
                "resource": f"memory:{bot}:{note_id}",
                "title": change.get("title") or change.get("field") or "关联记忆",
                "before": change.get("before") if historical else None,
                "after": change.get("after") if historical else None,
                "historical": historical,
                "sources": sources,
                "evidence": evidence,
            }
        )
    reasons = []
    states = {"active": "生效", "archived": "已归档", "quarantine": "待核对", None: "未记录"}
    for change in snapshots:
        label = change.get("title") or change.get("field") or "记忆"
        before, after = change.get("before_state"), change.get("after_state")
        if before is not None and before != after:
            reasons.append(
                f"{label}：状态由{states.get(before, '未知')}改为{states.get(after, '未知')}。"
            )
        before, after = change.get("before_fixed"), change.get("after_fixed")
        if before is not None and before != after:
            reasons.append(
                f"{label}：{'已人工固定，自动整理不能覆盖。' if after else '已解除人工固定。'}"
            )
    if item.get("kind") == "processing_failed":
        reasons.append(
            "来源依据或模型输出未通过校验。"
            if item.get("reason") == "invalid_evidence_or_output"
            else "整理执行失败，请检查运行日志及模型连接。"
        )
    if not snapshots and item.get("notes"):
        reasons.append("这条旧活动没有保存当时的前后内容，无法可靠还原；关联链接打开的是当前记忆。")
    result.update(changes=changes, reasons=reasons)
    return result
