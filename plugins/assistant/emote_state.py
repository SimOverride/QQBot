"""统一表情反馈工具；旧表结构仅供私有包迁移读取。"""

from .emote_feedback import associated, feedback_note, save_feedback, verdict
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
        raise ValueError("表情反馈参数无效")
    bot, scope, user = session.key
    action = args["action"]
    if action not in ("read", "remember", "disable"):
        raise ValueError("表情反馈操作无效")
    image_digest = session.emotes.digest(args["id"])
    note = feedback_note(session.emotes, bot, image_digest, user)
    if action == "read":
        return {
            "status": "ok",
            "revision": note["revision"] if note else "",
            "fixed": bool(note and note.get("fixed")),
            **session.emotes.experience(bot, image_digest, user, session.archive),
        }
    if (
        not isinstance(args["reason"], str)
        or not 1 <= len(args["reason"].strip()) <= 6000
        or not isinstance(args["message_ids"], list)
        or len(args["message_ids"]) != 1
        or type(args["message_ids"][0]) is not int
    ):
        raise ValueError("每次使用一条可归因的实际反馈，不能混合不同人的意见")
    row = session.archive.db.execute(
        "SELECT * FROM messages WHERE bot=? AND grp=? AND message_id=? AND usr=?",
        (bot, scope, args["message_ids"][0], user),
    ).fetchone()
    if row is None:
        return {"status": "invalid_feedback"}
    send = associated(session.emotes, row, image_digest)
    if send is None:
        return {"status": "unlinked_feedback", "hint": "反馈须明确对应成功发送的该表情"}
    decision = verdict(row["content"])
    if action == "disable" and (decision is None or not decision[1]):
        return {"status": "invalid_feedback", "hint": "停用需要明确的不要再发要求"}
    with migration.project_lock(session.archive.root, name=".migration.lock"):
        if not getattr(session, "is_current", lambda: True)():
            return {"status": "superseded"}
        # 只保存原始反馈及标注为主观的解读，模型文字不能取代用户证据。
        return save_feedback(
            session.emotes,
            session.archive,
            row,
            send,
            "实际反馈：" + row["content"][:1200] + "\n主观使用建议：" + args["reason"].strip(),
            decision[0] if decision else "uncertain",
            action == "disable" or bool(decision and decision[1]),
            args["revision"],
        )
