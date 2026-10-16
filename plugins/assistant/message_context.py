"""归档与模型上下文共用消息归属和引用资料。"""

from .shares import message_text


def mentions_all(event):
    """只识别当前消息的原生全体提醒，引用和普通文字不提高关注度。"""
    return any(
        segment.type == "at" and str(segment.data.get("qq", "")) == "all"
        for segment in getattr(event, "original_message", ())
    )


def event_metadata(event):
    sender = getattr(event, "sender", None)
    mentions = []
    reply_id = None
    for segment in getattr(event, "original_message", ()):
        if segment.type == "at":
            value = str(segment.data.get("qq", ""))
            if value == "all" or value.isdecimal():
                mentions.append("all" if value == "all" else int(value))
        elif segment.type == "reply":
            value = str(segment.data.get("id", ""))
            if value.lstrip("-").isdigit():
                reply_id = int(value)
    reply = getattr(event, "reply", None)
    quoted_sender = getattr(reply, "sender", None)
    quoted_message = getattr(reply, "message", None)
    return {
        **({"poke": event.poke} if getattr(event, "poke", None) else {}),
        "sender_name": getattr(sender, "card", None) or getattr(sender, "nickname", None),
        "sender_nickname": getattr(sender, "nickname", None),
        "sender_role_at_send": getattr(sender, "role", None),
        "mentions": mentions,
        "reply_to": {
            "message_id": getattr(reply, "message_id", reply_id),
            "sender": getattr(quoted_sender, "user_id", None),
            "sender_name": (
                getattr(quoted_sender, "card", None) or getattr(quoted_sender, "nickname", None)
            ),
            "text": message_text(quoted_message)[:1600] if quoted_message else None,
        }
        if reply is not None or reply_id is not None
        else None,
    }
