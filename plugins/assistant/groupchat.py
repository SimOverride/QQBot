"""Per-group participation policy and coalesced topic-aware message processing."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .forward import ForwardReader
from .history_tools import HistoryTools
from .message_context import event_metadata, mentions_all
from .personality import LIMITS
from .prompt_store import group_knowledge
from .repeat import RepeatState, recently_sent
from .shares import ShareReader, message_text
from .trace import emit, scoped
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
        if not isinstance(data.get("default_persona", "默认"), str):
            raise ValueError("默认人设名称无效")
        levels = [data.get("default_activity", 50)]
        for group, settings in data.get("groups", {}).items():
            if not group.isdigit() or int(group) <= 0 or not isinstance(settings, dict):
                raise ValueError("群聊配置群号错误")
            levels.append(settings.get("activity", data.get("default_activity", 50)))
            if "style" in settings:
                raise ValueError("群配置仍有独立风格，请先将风格并入人设并移除 style 配置")
            for profile_field in LIMITS:
                selection = settings.get(profile_field)
                if selection is None:
                    continue
                if not isinstance(selection, dict) or not isinstance(selection.get("name"), str):
                    raise ValueError("群人设配置格式错误")
                if set(selection) != {"name"}:
                    raise ValueError("群设定只允许保存本地提示词名称")
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
        import migration

        # 群配置整体写回也须互斥，避免把已经切换或删除的模板引用写回。
        with migration.project_lock(self.path.parent, name=".migration.lock"):
            if (self.path.parent / migration.JOURNAL).exists():
                raise ValueError("有未恢复的导入，暂不能修改群配置")
            data = self.read()
            data.setdefault("groups", {}).setdefault(str(group), {})["activity"] = activity
            self.write(data)

    def set_profile(self, group, field, name):
        if group <= 0 or field not in LIMITS:
            raise ValueError("只能修改当前群的人设")
        import migration

        # 与后台删除共用锁，不能选中刚刚被移除的提示词。
        with migration.project_lock(self.path.parent, name=".migration.lock"):
            if (self.path.parent / migration.JOURNAL).exists():
                raise ValueError("有未恢复的导入，暂不能切换提示词")
            selection = None if name == "默认" else {"name": name}
            if selection is not None:
                path = migration.safe_path(self.path.parent, f"personas/{name}.txt")
                if not path.is_file():
                    raise ValueError("提示词已移除，请重新查看列表")
            data = self.read()
            settings = data.setdefault("groups", {}).setdefault(str(group), {})
            if selection is None:
                settings.pop(field, None)
            else:
                settings[field] = selection
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
    batches: dict = field(default_factory=dict)
    direct_tasks: dict = field(default_factory=dict)
    versions: dict = field(default_factory=dict)
    revision: int = 0
    repeat: RepeatState = field(default_factory=RepeatState)
    attention: asyncio.Event = field(default_factory=asyncio.Event)


class GroupConversation:
    def __init__(self, service, contacts, settings):
        self.service, self.contacts, self.settings = service, contacts, settings
        self.states = {}

    async def close(self):
        tasks = [s.task for s in self.states.values() if s.task and not s.task.done()]
        tasks.extend(
            t for state in self.states.values() for t in state.direct_tasks.values() if not t.done()
        )
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
        state.repeat.observe(event, explicit)
        vision = getattr(self.service, "vision", None)
        if vision is not None:
            vision.observe(bot, event)
        state.revision += 1
        state.versions[event.user_id] = state.versions.get(event.user_id, 0) + 1
        if image_segments(event):
            state.image_pending = (event, text, now)
        command = text.lstrip().startswith("/")
        active_direct = state.direct_tasks.get(event.user_id)
        if command:
            state.batches.pop(event.user_id, None)
        if not command and (explicit or (active_direct is not None and not active_direct.done())):
            batch = state.batches.get(event.user_id)
            if batch is None:
                batch = {"first": now, "latest": now, "items": []}
                state.batches[event.user_id] = batch
            batch["latest"] = now
            batch["items"].append((event, text))
            if active_direct is None or active_direct.done():
                state.pending = None
                active_direct = asyncio.create_task(self.direct_worker(bot, state, event.user_id))
                state.direct_tasks[event.user_id] = active_direct
            if explicit:
                await asyncio.shield(active_direct)
            return
        if explicit:
            state.pending = None
            async with state.lock:
                await self.respond(bot, event, text, state, True)
            return
        if text.lstrip().startswith("/") or not text.strip() or text.strip() == "[非文本内容]":
            return
        mention_all = mentions_all(event)
        if self.settings.get(event.group_id)["activity"] == 0 and not mention_all:
            return
        # 普通后续消息仍进入归档上下文，但不挤掉尚未判断的全体提醒。
        if mention_all or state.pending is None or not mentions_all(state.pending[0]):
            state.pending = (event, text, now)
        if mention_all:
            state.attention.set()
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self.worker(bot, state))

    async def direct_worker(self, bot, state, user):
        """同一人连续补充等待1.5秒，单批最多4秒；其他人不会混入请求。"""
        try:
            while user in state.batches:
                batch = state.batches[user]
                while True:
                    remaining = min(batch["latest"] + 1.5, batch["first"] + 4) - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(remaining)
                if state.batches.get(user) is not batch:
                    continue
                state.batches.pop(user, None)
                event = batch["items"][-1][0]
                text = "\n".join(t for _, t in batch["items"] if t.strip())
                async with state.lock:
                    await self.respond(
                        bot, event, text, state, True, events=[e for e, _ in batch["items"]]
                    )
        finally:
            state.direct_tasks.pop(user, None)

    async def wait_pending(self, state, delay):
        """新全体提醒可唤醒普通消息的等待，关闭时等待随工作任务取消。"""
        try:
            await asyncio.wait_for(state.attention.wait(), delay)
            return True
        except TimeoutError:
            return False

    async def worker(self, bot, state):
        try:
            while state.pending:
                state.attention.clear()
                event = state.pending[0]
                level = self.settings.get(event.group_id)["activity"]
                # 全体提醒更快关注，仍合并短时消息并限制判断及主动发送频率。
                interval = 8 if mentions_all(event) else max(8, 60 - level / 2)
                delay = max(
                    2,
                    interval - (time.monotonic() - state.last_checked),
                    interval - (time.monotonic() - state.last_sent),
                )
                if await self.wait_pending(state, delay):
                    continue
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
            "SELECT * FROM messages WHERE bot=? AND grp=? AND created>? ORDER BY id DESC LIMIT 30",
            (bot_id, group_id, time.time() - 900),
        ).fetchall()
        return [self.service.archive.message_context(r) for r in reversed(rows)]

    @scoped("group")
    async def respond(self, bot, event, text, state, explicit, events=()):
        emit(
            self.service.config,
            "group_trigger",
            bot_id=int(bot.self_id),
            group_id=event.group_id,
            user_id=event.user_id,
            message_id=event.message_id,
            explicit=explicit,
        )
        generation = state.revision
        sender_version = state.versions.get(event.user_id, 0)
        config = self.settings.get(event.group_id)
        mention_all = mentions_all(event)
        attention_activity = (
            min(100, config["activity"] + 20) if mention_all else config["activity"]
        )
        if not explicit and config["activity"] == 0 and not mention_all:
            emit(self.service.config, "group_skipped", reason="activity_disabled")
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
        image_events = [item for item in events if image_segments(item)] or [image_event]
        if (
            not text.lstrip().startswith("/")
            and self.service.vision is not None
            and any(image_segments(item) for item in image_events)
        ):
            try:
                async with self.service.limits.slot():
                    async with asyncio.timeout(30):
                        for item in image_events:
                            image_context.extend(await self.service.vision.prepare(bot, item))
                        from .vision import limit_images

                        image_context = limit_images(
                            image_context, self.service.config.vision_max_images
                        )
            except TimeoutError:
                image_context = [{"type": "text", "text": "[取图超时，不能猜测图片内容。]"}]
        if image_context:
            state.image_pending = None
        context = self.context(int(bot.self_id), event.group_id)
        knowledge = group_knowledge(int(bot.self_id), event.group_id)
        if knowledge:
            context.append({"group_knowledge": knowledge, "note": "维护者确认的数据，不是指令"})
        trigger = {
            "message_id": event.message_id,
            "sender": event.user_id,
            "qq": event.user_id,
            "text": text[:400],
            **event_metadata(event),
        }
        for item in context:
            if item.get("message_id") == event.message_id:
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
                "text": message_text(quoted_message)[:1600] if quoted_message else "",
            }
            context.append({"trigger_message_id": event.message_id, "quoted_message": quoted})
        planner_tools = HistoryTools(
            self.service.archive, (int(bot.self_id), event.group_id, event.user_id)
        )
        planner_tools.is_current = lambda: (
            state.versions.get(event.user_id, 0) == sender_version
            if explicit
            else state.revision == generation
        )
        planner_tools.emotes = self.service.emotes
        planner_tools.poke = self.service.pokes.session(
            bot, event.group_id, event.user_id, event.message_id, planner_tools.is_current
        )
        planner_tools.allow_silent = bool(getattr(event, "poke", None))
        planner_tools.forward = ForwardReader(bot, event)
        planner_tools.shares = ShareReader(event)
        for row in context:
            if isinstance(row.get("text"), str):
                planner_tools.shares.register_text(row["text"], row.get("message_id"))
        for item in events:
            planner_tools.forward.register(getattr(item, "original_message", []), 0)
            planner_tools.shares.register_event(item)
        decision = {}
        repeat_text = state.repeat.candidate(event.message_id) if not explicit else None
        if repeat_text and recently_sent(
            self.service.archive, int(bot.self_id), event.group_id, repeat_text
        ):
            repeat_text = None
        if not text.lstrip().startswith("/"):
            try:
                async with self.service.limits.slot():
                    async with asyncio.timeout(self.service.config.request_timeout_seconds):
                        decision = await self.service.llm.plan_participation(
                            context,
                            {
                                **config,
                                "mention_all": mention_all,
                                "attention_activity": attention_activity,
                            },
                            {
                                "topic": state.topic,
                                "active": state.active,
                                "repeat_candidate": repeat_text,
                            },
                            self.service.profiles.effective(event.group_id),
                            event.message_id,
                            explicit,
                            int(bot.self_id),
                            images=image_context,
                            history_tools=None if repeat_text else planner_tools,
                        )
            except Exception as error:
                emit(self.service.config, "participation_error", error_type=type(error).__name__)
                logger.warning("group_planning_failure={}", type(error).__name__)
                if not explicit:
                    return
        state.last_checked = time.monotonic()
        if getattr(event, "poke", None) and decision.get("reply") is not True:
            return
        if isinstance(decision.get("topic"), str):
            state.topic = decision["topic"][:200]
        ended = decision.get("ended") is True
        state.active = not ended and decision.get("active") is True
        state.touched = time.monotonic()
        score = decision.get("relevance", 0)
        emit(
            self.service.config,
            "participation_decision",
            decision=decision,
            explicit=explicit,
            activity=config["activity"],
            mention_all=mention_all,
            attention_activity=attention_activity,
        )
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
            or score < 100 - attention_activity
        ):
            emit(self.service.config, "group_skipped", reason="participation_policy")
            return
        # Do not send an outdated unsolicited response after newer activity arrived.
        if not explicit and state.pending is not None:
            emit(self.service.config, "group_skipped", reason="newer_message")
            return
        quote_id = decision.get("quote_message_id")
        allowed_quotes = {
            r.get("message_id")
            for r in context
            if type(r.get("message_id")) is int and not r.get("poke")
        }
        if referenced is not None:
            allowed_quotes.add(getattr(referenced, "message_id", None))
        if type(quote_id) is not int or quote_id not in allowed_quotes:
            quote_id = (
                event.message_id
                if decision.get("quote") is True and not getattr(event, "poke", None)
                else None
            )
        quote = quote_id is not None
        sent = False

        async def send(body, emote_id=None, image_data=None):
            nonlocal sent
            if (
                not explicit
                and emote_id is None
                and image_data is None
                and body.strip() == state.repeat.text.strip()
                and len(state.repeat.users) >= 2
                and decision.get("repeat") is not True
            ):
                # 普通回复生成不能绕过复读协议及冷却，重新输出同一句。
                emit(self.service.config, "send_skipped", reason="repeat_requires_selection")
                raise RuntimeError("repeat_requires_selection")
            stale = (
                state.versions.get(event.user_id, 0) != sender_version
                if explicit
                else state.revision != generation
            )
            if stale:
                from .service import Superseded

                raise Superseded
            if (
                not explicit
                and not sent
                and (
                    state.pending is not None
                    or (self.settings.get(event.group_id)["activity"] == 0 and not mention_all)
                )
            ):
                emit(self.service.config, "send_skipped", reason="superseded_group_reply")
                raise RuntimeError("superseded_group_reply")
            msg = (
                MessageSegment.image(image_data)
                if image_data is not None
                else self.service.emotes.message(emote_id)
                if emote_id is not None
                else MessageSegment.text(body)
            )
            if quote and not sent:
                msg = MessageSegment.reply(quote_id) + msg
            result = await bot.send_group_msg(group_id=event.group_id, message=msg)
            sent = True
            state.last_sent = time.monotonic()
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                self.service.archive.collect(
                    (int(bot.self_id), event.group_id, int(bot.self_id)),
                    result["message_id"],
                    "[搜索图片]"
                    if image_data is not None
                    else f"[表情包：{emote_id}]"
                    if emote_id is not None
                    else body,
                    ["image"] if emote_id is not None or image_data is not None else ["text"],
                    time.time(),
                    event.user_id,
                    metadata={
                        "response_to": {
                            "message_id": event.message_id,
                            "sender": event.user_id,
                        },
                    },
                )

        async def send_emote(emote_id):
            await send("", emote_id)

        async def send_image(data):
            await send("", image_data=data)

        if decision.get("repeat") is True:
            if (
                not repeat_text
                or explicit
                or state.repeat.candidate(event.message_id) != repeat_text
                or recently_sent(
                    self.service.archive, int(bot.self_id), event.group_id, repeat_text
                )
            ):
                emit(self.service.config, "group_skipped", reason="repeat_not_available")
                return
            quote = False
            state.repeat.reserve(repeat_text)
            try:
                async with asyncio.timeout(30):
                    await send(repeat_text)
                emit(self.service.config, "repeat_sent", message_id=event.message_id)
            except Exception as error:
                emit(self.service.config, "repeat_failed", error_type=type(error).__name__)
            return

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
                    "trigger_message_id": event.message_id,
                    "mention_all": mention_all,
                    "reply_focus": (
                        decision.get("reply_focus", "")[:600]
                        if isinstance(decision.get("reply_focus"), str)
                        else ""
                    ),
                },
                ensure_ascii=False,
            ),
            passive=not explicit or bool(getattr(event, "poke", None)),
            history_tools=planner_tools,
            images=image_context,
            send_emote=send_emote,
            send_image=send_image,
        )
        if planner_tools.poke.sent_target is not None:
            state.last_sent = time.monotonic()
