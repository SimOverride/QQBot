"""Framework-independent message orchestration, including send-before-save."""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable

from nonebot.log import logger

from .commands import Commands, RoleLookup
from .config import Config
from .history_tools import HistoryTools
from .http import ServiceError
from .identity import identity_context
from .limits import Limits
from .llm import LLM, render_reply
from .longterm import LongTermMemory
from .memory import Memory
from .notices import Notices
from .personality import ProfileStore
from .poke import Pokes
from .trace import emit, scoped


class Superseded(Exception):
    """收到补充消息后停止旧回复，不向群里发送错误提示。"""


class SendFailure(Exception):
    pass


class Assistant:
    def __init__(
        self,
        config: Config,
        llm: LLM,
        profiles: ProfileStore | None = None,
        archive: LongTermMemory | None = None,
    ):
        self.config, self.llm = config, llm
        self.archive = archive
        self.vision = None
        self.emotes = None
        self.pokes = Pokes(archive)
        self.memory = Memory(config, archive)
        self.profiles = profiles if profiles is not None else ProfileStore()
        self.commands = Commands(config, self.memory)
        self.commands.profiles = self.profiles
        self.limits = Limits(config)
        self.notices = Notices(llm, self.profiles, self.limits)

    @scoped("request")
    async def handle(
        self,
        bot_id: int,
        group_id: int,
        user_id: int,
        message_id: int,
        text: str,
        send: Callable[[str], Awaitable[None]],
        role_lookup: RoleLookup | None = None,
        *,
        private_allowed: bool = False,
        names: dict[int, str] | None = None,
        group_context: str = "",
        passive: bool = False,
        history_tools=None,
        image_reader=None,
        images=None,
        send_emote=None,
        send_image=None,
    ) -> None:
        cfg = self.config
        emit(
            cfg,
            "request_received",
            bot_id=bot_id,
            group_id=group_id,
            user_id=user_id,
            message_id=message_id,
            passive=passive,
        )
        if (group_id <= 0 and not (private_allowed and group_id == -user_id)) or bot_id == user_id:
            return
        from .events import Events

        events = Events(self.archive.root, (bot_id, group_id)) if self.archive else None
        self.cleanup()
        if self.limits.duplicate((bot_id, group_id, message_id)):
            emit(cfg, "request_skipped", reason="duplicate")
            return
        if self.archive and self.archive.seen(bot_id, group_id, message_id):
            emit(cfg, "request_skipped", reason="already_sent")
            return
        request_id = uuid.uuid4().hex[:12]
        emit(cfg, "request_link", request_id=request_id)
        started = time.monotonic()
        key = (bot_id, group_id, user_id)
        text = self.commands.entry_command(text.strip())
        raw_send = send
        generating_reply = False

        async def send(text: str):
            if passive and not generating_reply:
                emit(cfg, "send_skipped", reason="passive_notice")
                return
            try:
                await asyncio.wait_for(raw_send(text), 30)
                emit(cfg, "send_result", status="sent", text=text, chars=len(text))
            except Superseded:
                raise
            except Exception as error:
                emit(cfg, "send_result", status="failed", error_type=type(error).__name__)
                raise SendFailure from None

        async def send_notice(body: str):
            if passive:
                return
            await send(await self.notices.render(body, group_id))

        try:
            if not text:
                await send_notice("目前没有可读取的文字内容，请发送文字消息。")
                return
            if self.limits.pending >= cfg.max_pending_requests:
                emit(cfg, "request_skipped", reason="queue_full")
                await send_notice("当前请求较多，请稍后重试。")
                return
            if not text.startswith("/") and self.limits.cooling(key):
                emit(cfg, "request_skipped", reason="cooldown")
                await send_notice("提问太快了，请稍等几秒再试。")
                return
            self.limits.pending += 1
            try:
                if text.startswith("/"):
                    result = await self.commands.run(key, text, role_lookup)
                    if text.split()[0] in ("/后台", "/任务") or (
                        self.commands.extensions is not None
                        and self.commands.extensions.is_command(text.split()[0])
                    ):
                        # 入口及权限结果原样发送，不交给模型改写。
                        await send(result)
                    else:
                        result = await self.notices.render(result, group_id)
                        await send(result)
                    # 只记录实际发送成功的命令结果，后续问答可获知操作已完成或被拒绝。
                    if not passive:
                        async with self.memory.locked(key):
                            if self.archive:
                                self.archive.save(key, message_id, text, result)
                            self.memory.save(key, text, result)
                    return
                async with self.memory.locked(key):
                    async with self.limits.slot():
                        async with asyncio.timeout(cfg.request_timeout_seconds):
                            if image_reader:
                                images = await image_reader(request_id)
                            if self.archive and history_tools is None:
                                history_tools = HistoryTools(self.archive, key)
                            if history_tools is not None and send_emote is not None:
                                history_tools.emotes = self.emotes
                            if history_tools is not None and self.commands.tasks is not None:
                                history_tools.tasks = self.commands.tasks.session(
                                    key, role_lookup, str(message_id)
                                )
                            if history_tools is not None and self.commands.extensions is not None:
                                history_tools.extensions = (
                                    self.commands.extensions.session(key, role_lookup)
                                    if group_id > 0
                                    else None
                                )
                            group_role = None
                            if group_id > 0 and role_lookup is not None:
                                try:
                                    role = await asyncio.wait_for(role_lookup(), 8)
                                    if role in ("owner", "admin", "member"):
                                        group_role = role
                                except Exception:
                                    pass
                            if history_tools is not None:
                                from .maintenance import Maintenance
                                from .response import Response

                                history_tools.maintenance = Maintenance(cfg, key, text)

                                if not hasattr(history_tools, "response"):
                                    history_tools.response = Response(set())
                                history_tools.allow_silent = True
                                history_tools.events = events
                            reply = await self.llm.reply(
                                self.memory.history(key),
                                text,
                                request_id,
                                self.profiles.effective(group_id),
                                json.dumps(
                                    {
                                        "knowledge": self.archive.context(
                                            key, text, self.archive.recent(key)[1]
                                        )
                                        if self.archive
                                        else "",
                                        "group": group_context,
                                        "tasks": self.commands.tasks.context(key)
                                        if self.commands.tasks
                                        else "",
                                        "events": events.context() if events else "",
                                    },
                                    ensure_ascii=False,
                                ),
                                identity_context(
                                    cfg,
                                    bot_id,
                                    group_id,
                                    user_id,
                                    names,
                                    group_role,
                                    root=self.archive.root if self.archive else None,
                                ),
                                **({"images": images} if images else {}),
                                **(
                                    {
                                        "history_tools": history_tools
                                        or HistoryTools(self.archive, key)
                                    }
                                    if self.archive
                                    else {}
                                ),
                            )
                    if not getattr(history_tools, "is_current", lambda: True)():
                        raise Superseded
                    response = getattr(history_tools, "response", None)
                    if response is not None:
                        if getattr(history_tools, "observe_only", False):
                            response.state["action"] = "silent"
                        reply = response.apply(reply)
                    chunks = (
                        [reply.text]
                        if response is not None and response.state["action"] == "repeat"
                        else render_reply(reply, cfg)
                    )
                    emit(
                        cfg,
                        "reply_rendered",
                        original_chars=len(reply.text),
                        output_chars=sum(map(len, chunks)),
                        chunks=len(chunks),
                        truncated=any("回复过长，已截断" in chunk for chunk in chunks),
                    )
                    generating_reply = True
                    for index, chunk in enumerate(chunks):
                        if index:
                            await asyncio.sleep(0.35)
                        if not getattr(history_tools, "is_current", lambda: True)():
                            raise Superseded
                        await send(chunk)
                        if events:
                            events.publish(
                                "message_sent",
                                {"request": request_id, "index": index, "message_id": message_id},
                            )
                    saved_reply = "\n".join(chunks)
                    media = list(reply.media)
                    if not reply.media and reply.image_data is not None:
                        media.append({"image_data": reply.image_data})
                    if not reply.media and reply.emote_id is not None:
                        media.append({"emote_id": reply.emote_id})
                    media_sent = 0
                    for item in media:
                        if not getattr(history_tools, "is_current", lambda: True)():
                            raise Superseded
                        ident = item.get("emote_id")
                        try:
                            if ident is not None:
                                from .emote_state import current

                                if self.emotes is None or send_emote is None:
                                    raise ServiceError("当前会话无法发送表情包。")
                                if current(
                                    self.emotes, bot_id, group_id, self.emotes.digest(ident)
                                )["disabled"]:
                                    continue
                                await asyncio.wait_for(send_emote(ident), 30)
                                try:
                                    self.emotes.record_sent(ident, bot_id, group_id)
                                except Exception:
                                    logger.warning("emote_usage_write_failed")
                                saved_reply += f"\n[已发送表情包：{ident}]"
                            else:
                                if send_image is None:
                                    raise ServiceError("当前会话无法发送图片。")
                                await asyncio.wait_for(send_image(item["image_data"]), 30)
                                saved_reply += "\n[已发送搜索图片]"
                            media_sent += 1
                            emit(cfg, "send_result", status="sent", emote_id=ident)
                        except Superseded:
                            raise
                        except Exception as error:
                            emit(
                                cfg, "send_result", status="failed", error_type=type(error).__name__
                            )
                            raise SendFailure from None
                        await asyncio.sleep(0.35)
                    if events:
                        events.publish(
                            "reply_finished",
                            {
                                "request": request_id,
                                "message_id": message_id,
                                "text_messages_sent": len(chunks),
                                "image_sent": media_sent > 0,
                                "silent": not chunks and media_sent == 0,
                            },
                        )
                    if reply.save and (saved_reply or reply.poke_target is not None):
                        if reply.poke_target is not None:
                            saved_reply += f"\n[已拍一拍：QQ {reply.poke_target}]"
                        if self.archive:
                            try:
                                self.archive.save(key, message_id, text, saved_reply)
                            except Exception as error:
                                logger.warning(
                                    "request={} archive_failure={}",
                                    request_id,
                                    type(error).__name__,
                                )
                                await self.safe_send(
                                    send_notice,
                                    "回复已发送，但本次历史保存失败。",
                                    request_id,
                                )
                        self.memory.save(key, text, saved_reply)
                    maintenance = getattr(history_tools, "maintenance", None)
                    if maintenance:
                        if not getattr(history_tools, "is_current", lambda: True)():
                            raise Superseded
                        await maintenance.finish(
                            raw_send, getattr(history_tools, "is_current", lambda: True)
                        )
            finally:
                self.limits.pending -= 1
        except Superseded:
            if events:
                events.publish(
                    "superseded",
                    {
                        "request": request_id,
                        "message_id": message_id,
                        "hint": (
                            "旧回合未继续发送；已有工具结果可供后续请求继续，"
                            "是否补充或取消由主 agent 决定。"
                        ),
                    },
                )
            self.limits.cooldowns.pop(key, None)
            emit(cfg, "send_skipped", reason="newer_message")
        except ServiceError as error:
            emit(cfg, "request_error", error_type=type(error).__name__, reason=str(error))
            generating_reply = False
            await self.safe_send(
                send if "模型" in str(error) else send_notice, str(error), request_id
            )
        except TimeoutError:
            emit(cfg, "request_error", error_type="TimeoutError")
            generating_reply = False
            await self.safe_send(send_notice, "等待或处理超时，请稍后重试。", request_id)
        except SendFailure:
            if events:
                events.publish(
                    "send_failed",
                    {
                        "request": request_id,
                        "message_id": message_id,
                        "hint": "发送结果不确定，不自动重发。",
                    },
                )
            logger.warning("request={} send_failure", request_id)
        except Exception as error:
            emit(cfg, "request_error", error_type=type(error).__name__)
            generating_reply = False
            # Do not log exception text: it can contain full QQ events or HTTP headers.
            logger.warning("request={} failure={}", request_id, type(error).__name__)
            await self.safe_send(send_notice, "本次处理失败，请稍后重试。", request_id)
        finally:
            logger.info(
                "request={} mode=auto elapsed={:.2f}", request_id, time.monotonic() - started
            )

    @staticmethod
    async def safe_send(send, text: str, request_id: str):
        try:
            await send(text)
        except Exception as error:
            logger.warning("request={} send_failure={}", request_id, type(error).__name__)

    def cleanup(self):
        self.pokes.cleanup()
        self.commands.cleanup()
        self.memory.cleanup()
        self.limits.cleanup()
