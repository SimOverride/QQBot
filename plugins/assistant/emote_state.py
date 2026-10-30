"""统一表情反馈工具；旧表结构仅供私有包迁移读取。"""

from .emote_feedback import feedback_note, recent_sends, save_feedback
from .prompt_store import tool_definition

FUNCTION = tool_definition("manage_emote_memory")
SCHEMA = (
    "bot INTEGER NOT NULL, scope INTEGER NOT NULL, digest TEXT NOT NULL, "
    "disabled INTEGER NOT NULL, reason TEXT NOT NULL, memory TEXT NOT NULL, "
    "evidence TEXT NOT NULL, updated REAL NOT NULL, history TEXT NOT NULL, "
    "PRIMARY KEY(bot,scope,digest)"
)


def execute(session, args):
    import migration

    if not isinstance(args, dict) or set(args) != set(FUNCTION["parameters"]["required"]):
        raise ValueError("表情经验参数无效")
    bot, _, _ = session.key
    action, subject = args["action"], args["subject"]
    if action not in ("read", "remember", "disable", "enable"):
        raise ValueError("表情经验操作无效")
    if type(subject) is not int or subject < 0:
        raise ValueError("表情经验归属无效")
    image_digest = session.emotes.digest(args["id"])
    with migration.project_lock(session.archive.root, name=".migration.lock"):
        if (session.archive.root / migration.JOURNAL).exists():
            return {"status": "migration_in_progress"}
        if not getattr(session, "is_current", lambda: True)():
            return {"status": "superseded"}
        note = feedback_note(session.emotes, bot, image_digest, subject)
        if action == "read":
            return {
                "status": "ok",
                "revision": note["revision"] if note else "",
                "subject": subject,
                "value": note["value"] if note else "",
                "sends": recent_sends(session.emotes, bot, image_digest),
                **session.emotes.experience(bot, image_digest, subject, session.archive),
            }
        if (
            not isinstance(args["reason"], str)
            or not 1 <= len(args["reason"].strip()) <= 6000
            or not isinstance(args["revision"], str)
            or args["sentiment"] not in ("positive", "negative", "uncertain")
            or not isinstance(args["message_ids"], list)
            or any(type(ident) is not int for ident in args["message_ids"])
        ):
            raise ValueError("表情经验内容、倾向或来源参数无效")
        disabled = action == "disable" or (
            action == "remember" and bool(note and note.get("disabled"))
        )
        sentiment = {"disable": "avoid", "enable": "allow"}.get(action, args["sentiment"])
        return save_feedback(
            session.emotes,
            session.archive,
            bot,
            subject,
            image_digest,
            args["reason"].strip(),
            sentiment,
            disabled,
            args["revision"],
            args["message_ids"],
        )
