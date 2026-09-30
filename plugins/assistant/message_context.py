"""Platform message attribution shared by archival and model contexts."""


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
        "sender_name": getattr(sender, "card", None) or getattr(sender, "nickname", None),
        "sender_role_at_send": getattr(sender, "role", None),
        "mentions": mentions,
        "reply_to": {
            "message_id": getattr(reply, "message_id", reply_id),
            "sender": getattr(quoted_sender, "user_id", None),
            "sender_name": (
                getattr(quoted_sender, "card", None) or getattr(quoted_sender, "nickname", None)
            ),
            "text": quoted_message.extract_plain_text()[:400] if quoted_message else None,
        } if reply is not None or reply_id is not None else None,
    }
