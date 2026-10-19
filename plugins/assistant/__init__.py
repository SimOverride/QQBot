"""OneBot group entry point. Runtime resources are created only on startup."""

import asyncio
import re
import time
from contextlib import suppress
from pathlib import Path

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageSegment,
    PokeNotifyEvent,
    PrivateMessageEvent,
)
from nonebot.log import logger
from nonebot.message import event_preprocessor

from .config import Config
from .contacts import Contacts
from .emotes import Emotes
from .groupchat import GroupConversation, GroupSettings
from .history_tools import HistoryTools
from .longterm import LongTermMemory
from .message_context import event_metadata
from .personality import ProfileStore
from .poke import incoming
from .runtime import Runtime
from .search import Search
from .service import Assistant
from .shares import ShareReader, share_text
from .sharing import Sharing
from .vision import Vision


def text_mention(bot, names=()):
    """完整匹配机器人称呼，避免邮箱和名称前缀误触发。"""
    targets = {str(bot.self_id), *(name.strip() for name in names if name.strip())}
    choices = "|".join(re.escape(name) for name in sorted(targets, key=len, reverse=True))
    return re.compile(r"(?<![\w@＠])[@＠](?:" + choices + r")(?=$|\s|[^\w@＠])")


def addressed(bot: Bot, event: GroupMessageEvent, config: Config, names=()) -> bool:
    pattern = text_mention(bot, names)
    return str(event.user_id) != str(bot.self_id) and any(
        (seg.type == "at" and str(seg.data.get("qq")) == str(bot.self_id))
        or (seg.type == "text" and pattern.search(str(seg.data.get("text", ""))))
        for seg in event.original_message
    )


def extract_text(bot: Bot, event: GroupMessageEvent | PrivateMessageEvent, names=()) -> str:
    parts = []
    pattern = text_mention(bot, names) if getattr(event, "group_id", 0) > 0 else None
    for seg in event.original_message:
        if seg.type == "text":
            body = str(seg.data.get("text", ""))
            parts.append(pattern.sub("", body) if pattern else body)
        elif seg.type == "at" and str(seg.data.get("qq")) != bot.self_id:
            target = seg.data.get("qq")
            parts.append(" @全体成员 " if str(target) == "all" else f" @{target} ")
        elif seg.type == "image":
            parts.append(" [图片] ")
        elif seg.type in ("json", "xml", "music"):
            parts.append(share_text(seg) or " [非文本内容] ")
        elif seg.type not in ("at", "reply"):
            parts.append(" [非文本内容] ")
    text = "".join(parts)
    if not text.strip() and getattr(event, "reply", None) is not None:
        text = "[引用消息]"
    return text


def archive_event(archive: LongTermMemory, config: Config, bot: Bot, event: GroupMessageEvent):
    if event.group_id > 0:
        archive.collect(
            (int(bot.self_id), event.group_id, event.user_id),
            event.message_id,
            extract_text(bot, event),
            [seg.type for seg in event.original_message],
            event.time,
            metadata=event_metadata(event),
        )


def setup() -> None:
    driver = get_driver()
    config = get_plugin_config(Config)
    contacts = Contacts(config)
    service: Assistant | None = None
    client: httpx.AsyncClient | None = None
    cleaner: asyncio.Task | None = None
    directory_worker: asyncio.Task | None = None
    heartbeat_worker: asyncio.Task | None = None
    plugin_worker: asyncio.Task | None = None
    view_worker: asyncio.Task | None = None
    extensions = None
    tasks = None
    runtime = Runtime(Path(__file__).resolve().parents[2])
    archive: LongTermMemory | None = None
    sharing: Sharing | None = None
    conversations: GroupConversation | None = None

    @driver.on_startup
    async def startup():
        nonlocal service, client, cleaner, archive, sharing, conversations, directory_worker
        nonlocal heartbeat_worker, plugin_worker, extensions, tasks
        nonlocal view_worker
        config.validate_runtime()
        root = Path(__file__).resolve().parents[2]
        from ..extensions.manager import Manager as ExtensionManager
        from ..extensions.schedule import Schedule
        from ..tasks.runner import Runner
        from .dsh import Chat
        from .knowledge import read_directory

        Runner(root, config).package()
        contacts.directory_path = root / "data/contacts.json"
        contacts.directory = read_directory(root)
        profiles = ProfileStore(root / "personas" / "默认.txt")
        archive = LongTermMemory(root / "data" / "memory.sqlite3", config)
        client = httpx.AsyncClient(follow_redirects=False)
        service = Assistant(config, Chat(client, config, Search(client, config)), profiles, archive)
        extensions = ExtensionManager(root, service.commands, [Schedule()], archive)
        service.commands.extensions = extensions
        from ..tasks.generated import load as load_generated
        from ..tasks.manager import Manager as TaskManager

        load_generated(extensions, root)

        async def task_result(record):
            async with service.limits.slot():
                return await service.llm.task_result(record)

        tasks = TaskManager(root, service.commands, archive, extensions, task_result)
        service.commands.tasks = tasks

        async def plugin_loop():
            while True:
                await extensions.tick(get_bots())
                try:
                    await tasks.tick(get_bots())
                except Exception as error:
                    logger.warning("task_worker_failure={}", type(error).__name__)
                await asyncio.sleep(2)

        plugin_worker = asyncio.create_task(plugin_loop())

        async def view_loop():
            from .dsh import sync_views

            while True:
                try:
                    await sync_views(config)
                except Exception as error:
                    logger.warning("dsh_view_sync_failure={}", type(error).__name__)
                await asyncio.sleep(15)

        view_worker = asyncio.create_task(view_loop())

        service.vision = Vision(config)
        service.emotes = Emotes(
            root / config.emotes_dir, config.emotes_max_bytes, config.emotes_daily_import_limit
        )

        sharing = Sharing(service, contacts)
        settings = GroupSettings(root / "group_chat.json")
        service.commands.group_settings = settings
        profiles.group_settings = settings
        conversations = GroupConversation(service, contacts, settings)

        async def cleanup_loop():
            while True:
                await asyncio.sleep(60)
                service.cleanup()
                try:
                    await service.refresh_profiles()
                    for connected_bot in get_bots().values():
                        await sharing.tick(connected_bot)
                except Exception as error:
                    logger.warning("profile_worker_failure={}", type(error).__name__)

        cleaner = asyncio.create_task(cleanup_loop())

        async def directory_loop():
            while True:
                for connected_bot in get_bots().values():
                    try:
                        await contacts.sync_directory(connected_bot)
                    except Exception as error:
                        logger.warning("directory_refresh_failure={}", type(error).__name__)
                await asyncio.sleep(30)

        directory_worker = asyncio.create_task(directory_loop())

        async def heartbeat_loop():
            while True:
                try:
                    runtime.publish(get_bots())
                except OSError:
                    logger.warning("runtime_heartbeat_write_failed")
                await asyncio.sleep(5)

        heartbeat_worker = asyncio.create_task(heartbeat_loop())

    @driver.on_bot_connect
    async def sync_on_connect(bot: Bot):
        if extensions is not None:
            extensions.connected[bot.self_id] = time.time()
        try:
            runtime.publish(set(get_bots()) | {bot.self_id})
            await contacts.sync_directory(bot, force=True)
        except Exception as error:
            logger.warning("directory_refresh_failure={}", type(error).__name__)

    @driver.on_bot_disconnect
    async def record_disconnect(bot: Bot):
        if extensions is not None:
            extensions.connected.pop(bot.self_id, None)
        try:
            runtime.publish(set(get_bots()) - {bot.self_id})
        except OSError:
            logger.warning("runtime_heartbeat_write_failed")

    @driver.on_shutdown
    async def shutdown():
        if view_worker:
            view_worker.cancel()
            with suppress(asyncio.CancelledError):
                await view_worker
        if plugin_worker:
            plugin_worker.cancel()
            with suppress(asyncio.CancelledError):
                await plugin_worker
        if tasks:
            await tasks.close()
        if heartbeat_worker:
            heartbeat_worker.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_worker
        try:
            runtime.publish([], stopped=True)
        except OSError:
            logger.warning("runtime_heartbeat_write_failed")
        if directory_worker:
            directory_worker.cancel()
            with suppress(asyncio.CancelledError):
                await directory_worker
        if cleaner:
            cleaner.cancel()
            with suppress(asyncio.CancelledError):
                await cleaner
        if conversations:
            await conversations.close()
        if client:
            await client.aclose()
        if archive:
            archive.close()

    @event_preprocessor
    async def collect(bot: Bot, event: GroupMessageEvent):
        if archive is None:
            return
        # Capture all group messages before participation decisions.
        try:
            contacts.observe(int(bot.self_id), event.group_id, event.user_id, event.sender)
            archive_event(archive, config, bot, event)
        except Exception as error:
            logger.warning("group_archive_failure={}", type(error).__name__)

    async def rule(bot: Bot, event: GroupMessageEvent) -> bool:
        return str(event.user_id) != bot.self_id

    matcher = on_message(rule=rule, priority=10, block=True)

    @matcher.handle()
    async def handle(bot: Bot, event: GroupMessageEvent):
        if conversations is None:
            return
        if any(
            seg.type == "text" and any(c in str(seg.data.get("text", "")) for c in "@＠")
            for seg in event.original_message
        ):
            await contacts.refresh_group(bot, event.group_id)
        names = contacts.bot_names(int(bot.self_id), event.group_id)
        text = extract_text(bot, event, names)
        explicit = addressed(bot, event, config, names) or text.strip().split(maxsplit=1)[:1] == [
            "/积极性"
        ]
        try:
            await conversations.receive(bot, event, text, explicit)
        except Exception as error:
            logger.warning("group_receive_failure={}", type(error).__name__)

    async def private_rule(bot: Bot, event: PrivateMessageEvent) -> bool:
        if service is None or str(event.user_id) == bot.self_id:
            return False
        if event.user_id in config.bot_owners:
            return True
        allowed, _ = await contacts.private_access(bot, event.user_id)
        return allowed

    private_matcher = on_message(rule=private_rule, priority=10, block=True)

    @private_matcher.handle()
    async def handle_private(bot: Bot, event: PrivateMessageEvent):
        if service is None or archive is None:
            return
        bot_id, user_id = int(bot.self_id), event.user_id
        allowed, groups = await contacts.private_access(bot, user_id)
        allowed = allowed or user_id in config.bot_owners
        if not allowed:
            return
        scope = -user_id
        contacts.observe(bot_id, scope, user_id, event.sender)
        text = extract_text(bot, event)
        try:
            archive.collect(
                (bot_id, scope, user_id),
                event.message_id,
                text,
                ["poke"]
                if getattr(event, "poke", None)
                else [seg.type for seg in event.original_message],
                event.time,
                metadata=event_metadata(event),
            )
        except Exception as error:
            logger.warning("private_archive_failure={}", type(error).__name__)

        async def send_private(text, emote_id=None, image_data=None):
            message = (
                MessageSegment.image(image_data)
                if image_data is not None
                else service.emotes.message(emote_id)
                if emote_id is not None
                else MessageSegment.text(text)
            )
            result = await bot.send_private_msg(user_id=user_id, message=message)
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                archive.collect(
                    (bot_id, scope, bot_id),
                    result["message_id"],
                    "[搜索图片]"
                    if image_data is not None
                    else f"[表情包：{emote_id}]"
                    if emote_id is not None
                    else text,
                    ["image"] if emote_id is not None or image_data is not None else ["text"],
                    time.time(),
                    user_id,
                    metadata={"response_to": {"message_id": event.message_id, "sender": user_id}},
                )

        async def send_private_emote(emote_id):
            await send_private("", emote_id)

        async def send_private_image(data):
            await send_private("", image_data=data)

        async def read_images(request_id):
            return await service.vision.prepare(bot, event)

        async def send_to_group(group, body):
            review = await service.llm.review_group_send(
                text,
                {"id": group, "name": group_names.get(group, "")},
                body,
                history=service.memory.history((bot_id, scope, user_id)),
            )
            if not review["allowed"]:
                hints = {
                    "no_request": "未确认当前用户要求执行此次群发送，询问执行意图",
                    "ambiguous_target": "无法确定目标群，先查询群列表，仍不明确再询问",
                    "content_mismatch": "待发送内容不符合请求；此工具仅支持文字，不能发图",
                    "privacy": "待发送内容涉及私聊或个人隐私，不发送",
                    "invalid_review": "审核返回格式异常，未发送；不要声称目标群不明确",
                }
                return {
                    "status": "request_not_authorized",
                    "reason": review["reason"],
                    "hint": hints.get(review["reason"], "审核未通过，原因未提供，不猜测原因"),
                }
            _, current_groups = await contacts.private_access(bot, user_id)
            if group not in current_groups:
                return {"status": "access_denied"}
            with archive.db:
                inserted = archive.db.execute(
                    "INSERT OR IGNORE INTO directed_sends(bot,usr,message_id,status) "
                    "VALUES(?,?,?,?)",
                    (bot_id, user_id, event.message_id, "attempting"),
                ).rowcount
            if not inserted:
                return {"status": "already_attempted", "hint": "禁止重复发送，之前结果可能不明确"}
            try:
                result = await bot.send_group_msg(group_id=group, message=MessageSegment.text(body))
            except Exception:
                return {"status": "send_unknown", "hint": "发送结果不明，不重试，不声称成功"}
            with archive.db:
                archive.db.execute(
                    "UPDATE directed_sends SET status='sent'"
                    " WHERE bot=? AND usr=? AND message_id=?",
                    (bot_id, user_id, event.message_id),
                )
            if isinstance(result, dict) and type(result.get("message_id")) is int:
                try:
                    archive.collect(
                        (bot_id, group, bot_id),
                        result["message_id"],
                        body,
                        ["text"],
                        time.time(),
                        user_id,
                    )
                except Exception:
                    logger.warning("directed_send_archive_failure")
            return {"status": "sent", "group_id": group, "text": body}

        try:
            group_names = {
                int(g["group_id"]): str(g.get("group_name", ""))[:80]
                for g in await asyncio.wait_for(bot.get_group_list(), 8)
                if int(g["group_id"]) in groups
            }
        except Exception:
            group_names = {}
        history_tools = HistoryTools(
            archive,
            (bot_id, scope, user_id),
            groups,
            send_callback=send_to_group,
            group_names=group_names,
        )
        from .forward import ForwardReader

        history_tools.forward = ForwardReader(bot, event)
        history_tools.shares = ShareReader(event)
        history_tools.poke = service.pokes.session(bot, scope, user_id, event.message_id)
        history_tools.allow_silent = bool(getattr(event, "poke", None))
        await service.handle(
            bot_id,
            scope,
            user_id,
            event.message_id,
            text,
            send_private,
            private_allowed=True,
            passive=bool(getattr(event, "poke", None)),
            image_reader=read_images,
            send_emote=send_private_emote,
            send_image=send_private_image,
            history_tools=history_tools,
            names=contacts.for_scope(bot_id, scope),
        )

        if sharing and not history_tools.send_attempted and not getattr(event, "poke", None):
            row = archive.db.execute(
                "SELECT id FROM messages WHERE bot=? AND grp=? AND usr=? AND message_id=?",
                (bot_id, scope, user_id, event.message_id),
            ).fetchone()
            if row:
                try:
                    await sharing.review(bot, row[0])
                except Exception as error:
                    logger.warning("disclosure_review_failure={}", type(error).__name__)

    async def poke_rule(bot: Bot, event: PokeNotifyEvent) -> bool:
        return service is not None and incoming(bot, event) is not None

    poke_matcher = on_notice(rule=poke_rule, priority=10, block=True)

    @poke_matcher.handle()
    async def handle_poke(bot: Bot, event: PokeNotifyEvent):
        if service is None or conversations is None:
            return
        try:
            await service.pokes.dispatch(
                bot, event, contacts, config.bot_owners, conversations.receive, handle_private
            )
        except Exception as error:
            logger.warning("poke_receive_failure={}", type(error).__name__)
