"""Per-group participation policy and coalesced topic-aware message processing."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .history_tools import HistoryTools
from .message_context import event_metadata
from .vision import image_segments


class GroupSettings:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            self.write({"default_activity": 50, "groups": {}})
        self.read()

    def read(self):
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("groups", {}), dict):
            raise ValueError("群聊配置格式错误")
        levels = [data.get("default_activity", 50)]
        for group, settings in data.get("groups", {}).items():
            if not group.isdigit() or int(group) <= 0 or not isinstance(settings, dict):
                raise ValueError("群聊配置群号错误")
            levels.append(settings.get("activity", data.get("default_activity", 50)))
            interests = settings.get("interests", [])
            if (
                not isinstance(interests, list)
                or len(interests) > 20
                or any(not isinstance(x, str) or len(x) > 100 for x in interests)
            ):
                raise ValueError("群兴趣配置错误")
        if any(type(x) is not int or not 0 <= x <= 100 for x in levels):
            raise ValueError("积极性必须为0至100的整数")
        return data

    def get(self, group):
        data = self.read()
        settings = data.get("groups", {}).get(str(group), {})
        return {
            "activity": settings.get("activity", data.get("default_activity", 50)),
            "interests": settings.get("interests", []),
        }

    def write(self, data):
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(self.path)

    def set_activity(self, group, activity):
        if type(activity) is not int or not 0 <= activity <= 100:
            raise ValueError("积极性必须为0至100的整数")
        data = self.read()
        data.setdefault("groups", {}).setdefault(str(group), {})["activity"] = activity
        self.write(data)


@dataclass
class GroupState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: object = None
    task: asyncio.Task | None = None
    topic: str = ""
    active: bool = False
    touched: float = 0
    last_sent: float = 0
    last_checked: float = 0
    seen: dict = field(default_factory=dict)
    image_pending: tuple | None = None


class GroupConversation:
    def __init__(self, service, contacts, settings):
        self.service, self.contacts, self.settings = service, contacts, settings
        self.states = {}

    async def close(self):
        tasks = [s.task for s in self.states.values() if s.task and not s.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def receive(self, bot, event, text, explicit):
        if str(event.user_id) == bot.self_id:
            return
        state = self.states.setdefault((int(bot.self_id), event.group_id), GroupState())
        now = time.monotonic()
        state.seen = {mid: stamp for mid, stamp in state.seen.items() if now - stamp < 300}
        if event.message_id in state.seen:
            return
        state.seen[event.message_id] = now
        if image_segments(event):
            state.image_pending = (event, text, now)
        if explicit:
            state.pending = None
            async with state.lock:
                await self.respond(bot, event, text, state, True)
            return
        if text.lstrip().startswith("/") or not text.strip() or text.strip() == "[非文本内容]":
            return
        if self.settings.get(event.group_id)["activity"] == 0:
            return
        state.pending = (event, text, now)
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self.worker(bot, state))

    async def worker(self, bot, state):
        try:
            while state.pending:
                event = state.pending[0]
                level = self.settings.get(event.group_id)["activity"]
                # Batch bursts; bound both model polling and unsolicited output frequency.
                interval = max(8, 60 - level / 2)
                delay = max(
                    2,
                    interval - (time.monotonic() - state.last_checked),
                    interval - (time.monotonic() - state.last_sent),
                )
                await asyncio.sleep(delay)
                async with state.lock:
                    pending, state.pending = state.pending, None
                    if pending is None:
                        continue
                    event, text, arrival = pending
                    if time.monotonic() - arrival > 180:
                        continue
                    await self.respond(bot, event, text, state, False)
        except Exception as error:
            logger.warning("group_participation_failure={}", type(error).__name__)

    def context(self, bot_id, group_id):
        rows = self.service.archive.db.execute(
            "SELECT * FROM messages WHERE bot=? AND grp=? "
            "AND created>? ORDER BY id DESC LIMIT 30",
            (bot_id, group_id, time.time() - 900),
        ).fetchall()
        return [
            self.service.archive.message_context(r)
            for r in reversed(rows)
        ]

    async def respond(self, bot, event, text, state, explicit):
        config = self.settings.get(event.group_id)
        if not explicit and config["activity"] == 0:
            return
        if time.monotonic() - state.touched > 600:
            state.topic, state.active = "", False
        image_context = []
        image_event = event
        if (
            not image_segments(event)
            and state.image_pending is not None
            and state.image_pending[0].user_id == event.user_id
            and time.monotonic() - state.image_pending[2] < 90
        ):
            image_event = state.image_pending[0]
        if (
            not text.lstrip().startswith("/")
            and self.service.vision is not None
            and image_segments(image_event)
        ):
            try:
                async with self.service.limits.slot():
                    async with asyncio.timeout(30):
                        image_context = await self.service.vision.prepare(bot, image_event)
            except TimeoutError:
                image_context = [{"type": "text", "text": "[取图超时，不能猜测图片内容。]"}]
        if image_context:
            state.image_pending = None
        context = self.context(int(bot.self_id), event.group_id)
        trigger = {
            "message_id": event.message_id, "sender": event.user_id, "qq": event.user_id,
            "text": text[:400], **event_metadata(event),
        }
        for item in context:
            if item["message_id"] == event.message_id:
                item.update({k: v for k, v in trigger.items() if v is not None})
                break
        else:
            context.append(trigger)
        referenced = getattr(event, "reply", None)
        if referenced is not None:
            quoted_message = getattr(referenced, "message", None)
            quoted = {
                "message_id": getattr(referenced, "message_id", None),
                "qq": getattr(getattr(referenced, "sender", None), "user_id", None),
                "text": quoted_message.extract_plain_text()[:400] if quoted_message else "",
            }
            context.append({"trigger_message_id": event.message_id, "quoted_message": quoted})
        planner_tools = HistoryTools(
            self.service.archive, (int(bot.self_id), event.group_id, event.user_id)
        )
        planner_tools.emotes = self.service.emotes
        decision = {}
        if not text.lstrip().startswith("/"):
            try:
                async with self.service.limits.slot():
                    async with asyncio.timeout(self.service.config.request_timeout_seconds):
                        decision = await self.service.llm.plan_participation(
                            context,
                            config,
                            {"topic": state.topic, "active": state.active},
                            self.service.profiles.effective(),
                            event.message_id,
                            explicit,
                            int(bot.self_id),
                            images=image_context,
                            history_tools=planner_tools,
                        )
            except Exception as error:
                logger.warning("group_planning_failure={}", type(error).__name__)
                if not explicit:
                    return
        state.last_checked = time.monotonic()
        if isinstance(decision.get("topic"), str):
            state.topic = decision["topic"][:200]
        ended = decision.get("ended") is True
        state.active = not ended and decision.get("active") is True
        state.touched = time.monotonic()
        score = decision.get("relevance", 0)
        logger.info(
            "group_decision group={} explicit={} reply={} ended={} activity={}",
            event.group_id,
            explicit,
            decision.get("reply") is True,
            ended,
            config["activity"],
        )
        if not explicit and (
            decision.get("reply") is not True
            or ended
            or type(score) is not int
            or not 0 <= score <= 100
            or score < 100 - config["activity"]
        ):
            return
        # Do not send an outdated unsolicited response after newer activity arrived.
        if not explicit and state.pending is not None:
            return
        quote = decision.get("quote") is True
        sent = False

        async def send(body, emote_id=None):
            nonlocal sent
            if (
                not explicit
                and not sent
                and (
                    state.pending is not None or self.settings.get(event.group_id)["activity"] == 0
                )
            ):
                raise RuntimeError("superseded_group_reply")
            msg = (
                self.service.emotes.message(emote_id)
                if emote_id is not None
                else MessageSegment.text(body)
            )
            if quote and not sent:
                msg = MessageSegment.reply(event.message_id) + msg
            result = await bot.send_group_msg(group_id=event.group_id, message=msg)
            sent = True
            state.last_sent = time.monotonic()
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                self.service.archive.collect(
                    (int(bot.self_id), event.group_id, int(bot.self_id)),
                    result["message_id"],
                    f"[表情包：{emote_id}]" if emote_id is not None else body,
                    ["image"] if emote_id is not None else ["text"],
                    time.time(),
                    event.user_id,
                    metadata={
                        "response_to": {
                            "message_id": event.message_id, "sender": event.user_id,
                        },
                    },
                )

        async def send_emote(emote_id):
            await send("", emote_id)

        async def role_lookup():
            member = await bot.get_group_member_info(
                group_id=event.group_id, user_id=event.user_id, no_cache=True
            )
            if (
                int(member.get("group_id", 0)) != event.group_id
                or int(member.get("user_id", 0)) != event.user_id
            ):
                return "member"
            return member.get("role", "member")

        await self.contacts.refresh_group(bot, event.group_id)
        await self.service.handle(
            int(bot.self_id),
            event.group_id,
            event.user_id,
            event.message_id,
            "（用户仅@机器人，没有附加文字。）" if explicit and not text.strip() else text,
            send,
            role_lookup=role_lookup,
            names=self.contacts.for_scope(int(bot.self_id), event.group_id),
            group_context=json.dumps(
                {
                    "recent_group_messages": context,
                    "topic": state.topic,
                    "participating": state.active,
                },
                ensure_ascii=False,
            ),
            passive=not explicit,
            history_tools=planner_tools,
            images=image_context,
            send_emote=send_emote,
        )
