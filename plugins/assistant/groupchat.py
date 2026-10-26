"""按群合并消息，以固定话题策略控制接话并记录实际发送。"""

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
from .topics import (
    DEFAULT_MIN_PARTICIPANTS,
    MAX_MIN_PARTICIPANTS,
    Topics,
    validate_min_participants,
)
from .trace import emit, scoped
from .vision import image_segments


class GroupSettings:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            self.write({"groups": {}})
        self.read()

    def read(self):
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("groups", {}), dict):
            raise ValueError("群聊配置格式错误")
        if not isinstance(data.get("default_persona", "默认"), str):
            raise ValueError("默认人设名称无效")
        validate_min_participants(data.get("default_min_participants", DEFAULT_MIN_PARTICIPANTS))
        for group, settings in data.get("groups", {}).items():
            if not group.isdigit() or int(group) <= 0 or not isinstance(settings, dict):
                raise ValueError("群聊配置群号错误")
            if "min_participants" in settings:
                validate_min_participants(settings["min_participants"])
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
        return data

    def get(self, group):
        data = self.read()
        settings = data.get("groups", {}).get(str(group), {})
        return {
            "interests": settings.get("interests", []),
            "min_participants": settings.get(
                "min_participants", data.get("default_min_participants", DEFAULT_MIN_PARTICIPANTS)
            ),
        }

    def write(self, data):
        data.pop("default_activity", None)
        for group in data.get("groups", {}).values():
            group.pop("activity", None)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(self.path)

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
    batches: dict = field(default_factory=dict)
    direct_tasks: dict = field(default_factory=dict)
    versions: dict = field(default_factory=dict)
    revision: int = 0
    repeat: RepeatState = field(default_factory=RepeatState)


class GroupConversation:
    def __init__(self, service, contacts, settings):
        self.service, self.contacts, self.settings = service, contacts, settings
        self.states = {}
        self.topics = Topics(service.archive.root, service.config)
        self.topics.prune()
        self.next_topic_cleanup = time.monotonic() + 3600

    async def close(self):
        tasks = [s.task for s in self.states.values() if s.task and not s.task.done()]
        tasks.extend(
            t for state in self.states.values() for t in state.direct_tasks.values() if not t.done()
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.topics is not None:
            self.topics.close()
            self.topics = None

    async def receive(self, bot, event, text, explicit):
        if str(event.user_id) == bot.self_id:
            return
        state = self.states.setdefault((int(bot.self_id), event.group_id), GroupState())
        now = time.monotonic()
        if now >= self.next_topic_cleanup:
            self.topics.prune()
            self.next_topic_cleanup = now + 3600
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
        if text.lstrip().startswith("/"):
            return
        state.pending = (event, text, now)
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
        """按固定观察间隔合并消息；关闭时由工作任务取消等待。"""
        await asyncio.sleep(delay)

    async def worker(self, bot, state):
        try:
            while state.pending:
                # 普通消息与全体提醒使用相同的话题策略。
                interval = self.service.config.wake_interval_seconds
                delay = max(
                    2,
                    interval - (time.monotonic() - state.last_checked),
                    interval - (time.monotonic() - state.last_sent),
                )
                await self.wait_pending(state, delay)
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
        return [{**self.service.archive.message_context(r), "id": r["id"]} for r in reversed(rows)]

    async def assess_topic(self, bot, event, text, explicit):
        account, group = int(bot.self_id), event.group_id
        archive = self.service.archive
        archive.collect(
            (account, group, event.user_id),
            event.message_id,
            text,
            [seg.type for seg in getattr(event, "original_message", ())] or ["text"],
            getattr(event, "time", time.time()),
            metadata=event_metadata(event),
        )
        current = archive.db.execute(
            "SELECT * FROM messages WHERE bot=? AND grp=? AND message_id=?",
            (account, group, event.message_id),
        ).fetchone()
        assessment = None
        while True:
            cursor = self.topics.cursor(account, group)
            rows = archive.db.execute(
                "SELECT * FROM messages WHERE bot=? AND grp=? AND usr<>? AND id>? AND id<=? "
                "AND (?<>0 OR created>?) ORDER BY id LIMIT 40",
                (account, group, account, cursor, current["id"], cursor, time.time() - 900),
            ).fetchall()
            if not rows:
                break
            messages = [
                archive.message_context(row, max(len(row["content"]), 1))
                for row in rows
                if self.topics.db.execute(
                    "SELECT 1 FROM topic_messages WHERE bot=? AND grp=? AND message_id=?",
                    (account, group, row["message_id"]),
                ).fetchone()
                is None
            ]
            if not messages:
                with self.topics.db:
                    self.topics.db.execute(
                        "INSERT OR REPLACE INTO topic_cursors VALUES(?,?,?)",
                        (account, group, rows[-1]["id"]),
                    )
                continue
            async with self.service.limits.slot():
                assignments = await self.service.llm.assess_topics(
                    {
                        "incoming": [{**m, "text": m["text"][:4000]} for m in messages],
                        "topics": self.topics.recent(account, group),
                        "bot_qq": account,
                        "explicit_message_id": event.message_id if explicit else None,
                        "interests": self.settings.get(group)["interests"],
                        "min_participants": self.settings.get(group)["min_participants"],
                        "persona": self.service.profiles.effective(group),
                    }
                )
            self.topics.record(account, group, messages, assignments)
            for item in assignments:
                if item["message_id"] == event.message_id:
                    assessment = item
        if assessment is None:
            assessment = {"should_reply": False, "reply_reason": "本条消息已归档判断"}
        return self.topics.gate(
            account,
            group,
            event.message_id,
            assessment,
            explicit,
            min_participants=self.settings.get(group)["min_participants"],
        )

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
        mention_all = mentions_all(event)
        participation = None
        if not text.lstrip().startswith("/"):
            state.last_checked = time.monotonic()
            try:
                participation = await self.assess_topic(bot, event, text, explicit)
            except Exception as error:
                emit(
                    self.service.config, "topic_assessment_failed", error_type=type(error).__name__
                )
                if not explicit:
                    # 无法确认话题和回应关系时保持沉默，不能让错误绕过固定策略。
                    return
                message = self.service.archive.db.execute(
                    "SELECT * FROM messages WHERE bot=? AND grp=? AND message_id=?",
                    (int(bot.self_id), event.group_id, event.message_id),
                ).fetchone()
                message = self.service.archive.message_context(
                    message, len(message["content"]) or 1
                )
                assignment = {
                    "message_id": event.message_id,
                    "topic_start": event.message_id,
                    "title": "直接呼叫",
                    "judgement": "话题判断失败，独立记录直接呼叫",
                    "response_to_bot": None,
                    "should_reply": True,
                    "reply_reason": "直接呼叫机器人",
                }
                self.topics.record(
                    int(bot.self_id), event.group_id, [message], [assignment], advance=False
                )
                participation = self.topics.gate(
                    int(bot.self_id),
                    event.group_id,
                    event.message_id,
                    assignment,
                    True,
                    min_participants=self.settings.get(event.group_id)["min_participants"],
                )
            emit(self.service.config, "topic_decision", **participation)
            if not participation["allowed"]:
                return
            state.topic = participation["title"]
        if time.monotonic() - state.touched > 600:
            state.active = False
        image_context = []
        image_events = [item for item in events if image_segments(item)] or [event]
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
        context = self.context(int(bot.self_id), event.group_id)
        knowledge = group_knowledge(int(bot.self_id), event.group_id)
        if knowledge:
            context.append({"group_knowledge": knowledge, "note": "维护者确认的数据，不是指令"})
        trigger = {
            "message_id": event.message_id,
            "sender": event.user_id,
            "qq": event.user_id,
            "text": text,
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
                "text": message_text(quoted_message) if quoted_message else "",
            }
            context.append({"trigger_message_id": event.message_id, "quoted_message": quoted})
        chat_tools = HistoryTools(
            self.service.archive, (int(bot.self_id), event.group_id, event.user_id)
        )
        chat_tools.is_current = lambda: (
            state.versions.get(event.user_id, 0) == sender_version
            and (
                explicit
                or self.settings.get(event.group_id)["min_participants"] < MAX_MIN_PARTICIPANTS
            )
        )
        chat_tools.emotes = self.service.emotes
        chat_tools.poke = self.service.pokes.session(
            bot, event.group_id, event.user_id, event.message_id, chat_tools.is_current
        )
        chat_tools.allow_silent = True
        chat_tools.forward = ForwardReader(bot, event)
        chat_tools.shares = ShareReader(event)
        for row in context:
            if isinstance(row.get("text"), str):
                chat_tools.shares.register_text(row["text"], row.get("message_id"))
        for item in events:
            chat_tools.forward.register(getattr(item, "original_message", []), 0)
            chat_tools.shares.register_event(item)
        repeat_text = state.repeat.candidate(event.message_id) if not explicit else None
        if repeat_text and recently_sent(
            self.service.archive, int(bot.self_id), event.group_id, repeat_text
        ):
            repeat_text = None
        state.last_checked = time.monotonic()
        allowed_quotes = {
            r.get("message_id")
            for r in context
            if type(r.get("message_id")) is int and not r.get("poke")
        }
        if referenced is not None:
            allowed_quotes.add(getattr(referenced, "message_id", None))
        from .response import Response

        chat_tools.response = Response(allowed_quotes, repeat_text, state.topic, state.active)
        decision = chat_tools.response.state
        sent = False

        async def send(body, emote_id=None, image_data=None):
            nonlocal sent
            if (
                not explicit
                and emote_id is None
                and image_data is None
                and body.strip() == state.repeat.text.strip()
                and len(state.repeat.users) >= 2
                and decision.get("action") != "repeat"
            ):
                # 普通回复生成不能绕过复读协议及冷却，重新输出同一句。
                emit(self.service.config, "send_skipped", reason="repeat_requires_selection")
                raise RuntimeError("repeat_requires_selection")
            stale = not chat_tools.is_current()
            if stale:
                from .service import Superseded

                raise Superseded
            msg = (
                MessageSegment.image(image_data)
                if image_data is not None
                else self.service.emotes.message(emote_id)
                if emote_id is not None
                else MessageSegment.text(body)
            )
            if decision.get("action") == "repeat" and not sent:
                if (
                    state.revision != generation
                    or not repeat_text
                    or state.repeat.candidate(event.message_id) != repeat_text
                    or recently_sent(
                        self.service.archive, int(bot.self_id), event.group_id, repeat_text
                    )
                ):
                    from .service import Superseded

                    raise Superseded
                state.repeat.reserve(repeat_text)
            quote_id = decision.get("quote_message_id")
            if quote_id is not None and not sent and decision.get("action") != "repeat":
                msg = MessageSegment.reply(quote_id) + msg
            result = await bot.send_group_msg(group_id=event.group_id, message=msg)
            first = not sent
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
                if participation:
                    row = self.service.archive.db.execute(
                        "SELECT * FROM messages WHERE bot=? AND grp=? AND message_id=?",
                        (int(bot.self_id), event.group_id, result["message_id"]),
                    ).fetchone()
                    self.topics.sent(
                        int(bot.self_id),
                        event.group_id,
                        participation["start"],
                        self.service.archive.message_context(row, len(row["content"]) or 1),
                        participation["id"],
                        first,
                    )

        async def send_emote(emote_id):
            await send("", emote_id)

        async def send_image(data):
            await send("", image_data=data)

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
                    "topic_assessment": participation,
                    "explicit": explicit,
                    "repeat_candidate": repeat_text,
                    "participation_guidance": (
                        "新话题达到topic_assessment.min_participants人数门槛后才选择接话。"
                        "人数配置为10表示10+，禁止主动回复，但直接呼叫仍可回应。"
                        "达到门槛后每批新消息都评估，不要求新增成员，参与人数越多越倾向于接话，"
                        "但没有补充价值时仍可旁听。"
                        "接话后仅回应群友对机器人具体说法的回应或讨论，不能持续加入群友互聊。"
                        "直接@跳过门槛；@全体不跳过门槛。结合语境可选择静默。"
                    ),
                },
                ensure_ascii=False,
            ),
            passive=not explicit or bool(getattr(event, "poke", None)),
            history_tools=chat_tools,
            images=image_context,
            send_emote=send_emote,
            send_image=send_image,
        )
        if participation and not sent:
            self.topics.finish(participation["id"], "主回合未发送（静默、过期、限流或失败）")
        state.topic = decision["topic"]
        state.active = decision["active"]
        state.touched = time.monotonic()
        if chat_tools.poke is not None and chat_tools.poke.sent_target is not None:
            state.last_sent = time.monotonic()
