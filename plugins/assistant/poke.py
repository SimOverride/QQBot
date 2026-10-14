"""拍一拍通知与会话内发送；通知不是用户文字，也不授予额外权限。"""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message

from .emotes import tool

POKE_FUNCTION = tool(
    "poke_user",
    "在当前会话拍一拍别人。群聊只允许当前群成员，私聊只允许当前聊天对象。"
    "按明确请求、被拍后的互动或当前聊天气氛决定是否使用；每轮最多一次，不能拍机器人自己。"
    "user_id是目标QQ；只有返回sent才能声称已拍，结果不明不得重试。",
    {"user_id": {"type": "integer", "description": "目标QQ；昵称不明确时先查询，不猜测"}},
)


def incoming(bot, notice):
    """群通知使用user_id，私聊优先使用实际发送者sender_id，忽略自身通知回流。"""
    if (
        getattr(notice, "notice_type", None) != "notify"
        or getattr(notice, "sub_type", None) != "poke"
    ):
        return None
    try:
        own = int(bot.self_id)
        group = int(getattr(notice, "group_id", None) or 0)
        sender = int(getattr(notice, "sender_id", notice.user_id) if group == 0 else notice.user_id)
        target = int(notice.target_id)
        stamp = int(notice.time)
        if int(notice.self_id) != own or target != own or sender == own or sender <= 0 or group < 0:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    scope = group or -sender
    fingerprint = json.dumps([own, scope, sender, target, stamp]).encode()
    # 使用保留的负数范围作为本地事件编号，不与QQ消息编号混用，不能作为引用目标。
    ident = -(
        (1 << 62) | (int.from_bytes(hashlib.sha256(fingerprint).digest()[:8]) & ((1 << 62) - 1))
    )
    return SimpleNamespace(
        group_id=group,
        user_id=sender,
        message_id=ident,
        time=stamp,
        reply=None,
        sender=SimpleNamespace(),
        original_message=Message(f"[平台拍一拍通知：QQ {sender} 拍了拍机器人。不是用户文字。]"),
        poke={"sender": sender, "target": target},
    )


class Pokes:
    def __init__(self, archive=None):
        self.archive = archive
        self.received = {}
        self.cooldowns = {}

    def cleanup(self):
        now = time.monotonic()
        for mapping in (self.received, self.cooldowns):
            for key, expires in list(mapping.items()):
                if expires <= now:
                    del mapping[key]

    def receive(self, bot, notice):
        event = incoming(bot, notice)
        if event is None:
            return None
        self.cleanup()
        own = int(bot.self_id)
        scope = event.group_id or -event.user_id
        if self.archive:
            row = self.archive.db.execute(
                "SELECT 1 FROM messages WHERE bot=? AND grp=? AND message_id=?",
                (own, scope, event.message_id),
            ).fetchone()
            if row:
                return None
            self.archive.collect(
                (own, scope, event.user_id),
                event.message_id,
                str(event.original_message),
                ["poke"],
                event.time,
                metadata={"poke": event.poke},
            )
        key = (own, scope, event.user_id)
        if self.received.get(key, 0) > time.monotonic():
            return None
        self.received[key] = time.monotonic() + 10
        return event

    def session(self, bot, scope, user, message_id, current=lambda: True):
        return PokeSession(self, bot, scope, user, message_id, current)

    async def dispatch(self, bot, notice, contacts, owners, group_receive, private_receive):
        """私聊先按既有资格核验，群通知只交给发生通知的当前群。"""
        candidate = incoming(bot, notice)
        if candidate is None:
            return
        if not candidate.group_id and candidate.user_id not in owners:
            allowed, _ = await contacts.private_access(bot, candidate.user_id)
            if not allowed:
                return
        event = self.receive(bot, notice)
        if event is None:
            return
        if event.group_id:
            await group_receive(bot, event, str(event.original_message), True)
        else:
            await private_receive(bot, event)


class PokeSession:
    def __init__(self, owner, bot, scope, user, message_id, current):
        self.owner, self.bot = owner, bot
        self.scope, self.user, self.message_id = scope, user, message_id
        self.current = current
        self.attempted = False
        self.sent_target = None

    def check_current(self):
        if not self.current():
            from .service import Superseded

            raise Superseded

    def record(self, target, status, claim=False):
        """发送前在原消息元数据中占用一次机会，保存失败时不能发送。"""
        archive = self.owner.archive
        if not archive:
            return True
        key = (int(self.bot.self_id), self.scope, self.message_id)
        with archive.db:
            row = archive.db.execute(
                "SELECT metadata FROM messages WHERE bot=? AND grp=? AND message_id=?", key
            ).fetchone()
            if not row:
                return False
            data = json.loads(row[0] or "{}")
            if claim and "poke_action" in data:
                return False
            data["poke_action"] = {"target": target, "status": status}
            archive.db.execute(
                "UPDATE messages SET metadata=? WHERE bot=? AND grp=? AND message_id=?",
                (json.dumps(data, ensure_ascii=False), *key),
            )
        return True

    async def run(self, args):
        if (
            not isinstance(args, dict)
            or set(args) != {"user_id"}
            or type(args["user_id"]) is not int
            or not 0 < args["user_id"] < 2**63
        ):
            return {"status": "invalid_arguments"}
        target = args["user_id"]
        if target == int(self.bot.self_id):
            return {"status": "access_denied", "hint": "拍一拍工具用于拍别人"}
        if self.scope <= 0 and (self.scope != -self.user or target != self.user):
            return {"status": "access_denied", "hint": "私聊只能拍当前聊天对象"}
        if self.attempted:
            return {"status": "already_attempted", "hint": "每轮最多拍一次，不能重试"}
        self.check_current()
        if self.scope > 0:
            try:
                member = await asyncio.wait_for(
                    self.bot.get_group_member_info(
                        group_id=self.scope, user_id=target, no_cache=True
                    ),
                    8,
                )
                if (
                    int(member.get("group_id", 0)) != self.scope
                    or int(member.get("user_id", 0)) != target
                ):
                    return {"status": "access_denied"}
            except Exception:
                return {"status": "unavailable", "hint": "无法核实目标是否为当前群成员，未发送"}
        self.check_current()
        self.owner.cleanup()
        key = (int(self.bot.self_id), self.scope, target)
        if self.owner.cooldowns.get(key, 0) > time.monotonic():
            return {"status": "cooldown", "hint": "同一会话同一人30秒内最多拍一次，未发送"}
        try:
            claimed = self.record(target, "attempting", claim=True)
        except Exception:
            return {"status": "unavailable", "hint": "无法保存发送状态，未发送"}
        if not claimed:
            return {"status": "already_attempted", "hint": "原消息已尝试发送或缺失，不再次发送"}
        self.attempted = True
        self.owner.cooldowns[key] = time.monotonic() + 30
        params = {"user_id": target}
        if self.scope > 0:
            params["group_id"] = self.scope
        try:
            await asyncio.wait_for(self.bot.call_api("send_poke", **params), 8)
        except Exception:
            status = "send_unknown"
        else:
            status = "sent"
            self.sent_target = target
        try:
            self.record(target, status)
        except Exception:
            # 发送前已持久占用，不能因保存结果失败重发。
            pass
        return {
            "status": status,
            "user_id": target,
            "hint": (
                "平台已确认拍一拍发送成功"
                if status == "sent"
                else "发送结果不明，不重试，不声称成功"
            ),
        }
