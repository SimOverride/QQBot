"""Deterministic command permissions and single-use, scoped confirmations."""

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import Config
from .identity import permission_level
from .memory import Memory

RoleLookup = Callable[[], Awaitable[str]]


@dataclass
class Confirmation:
    token: str
    scope: str
    expires: float


class Commands:
    def __init__(self, config: Config, memory: Memory):
        self.config, self.memory = config, memory
        self.group_settings = None
        self.profiles = None
        self.pending: dict[tuple[int, int, int], Confirmation] = {}

    async def level(self, user_id: int, lookup: RoleLookup | None) -> int:
        if permission_level(self.config, user_id) == 2:
            return 2
        if lookup is None:
            return 0
        try:
            role = await asyncio.wait_for(lookup(), 8)
        except Exception:
            return 0
        return permission_level(self.config, user_id, role)

    async def run(self, key: tuple[int, int, int], text: str, lookup: RoleLookup | None) -> str:
        bot_id, group_id, user_id = key
        if group_id < 0:
            lookup = None
            return (
                (await self._run(key, text, lookup))
                .replace("你在本群", "你在私聊")
                .replace("本群的", "当前会话的")
                .replace("在本群发送 @机器人", "在当前会话发送")
                .replace("@我直接说话", "直接说话")
            )
        return await self._run(key, text, lookup)

    async def _run(self, key, text, lookup):
        bot_id, group_id, user_id = key
        parts = text.split()
        command, args = parts[0], parts[1:]
        if command in ("/人格", "/人设", "/风格"):
            if group_id <= 0:
                return "请在要调整的群中 @机器人 使用此指令，私聊不能修改群设定。"
            if await self.level(user_id, lookup) < 1:
                return "权限不足，仅本群管理员、群主或机器人所有者可管理本群人格和风格。"
            if self.group_settings is None or self.profiles is None:
                return "群配置未启用，未执行。"
            field = "style" if command == "/风格" else "persona"
            label = "风格" if field == "style" else "人格"
            try:
                if not args or args == ["列表"]:
                    selection = self.group_settings.read().get("groups", {}).get(
                        str(group_id), {}
                    ).get(field, {"name": "默认"})
                    return (
                        f"本群{label}：{selection['name']}\n"
                        + "可选：默认、" + "、".join(self.profiles.choices(field))
                        + f"\n{command} 切换 本地文件名（不含.txt）"
                        + f"\n{command} 默认 恢复本地默认"
                    )
                if args[0] == "设置":
                    return "聊天不能新增或编辑提示词，请在本地人格、风格文件夹管理后切换。"
                name = text.split(maxsplit=1)[1].strip()
                if args[0] == "切换":
                    values = text.split(maxsplit=2)
                    if len(values) != 3:
                        return f"用法：{command} 切换 本地文件名（不含.txt）。"
                    name = values[2].strip()
                if name != "默认":
                    self.profiles.read_selection(field, name)
                self.group_settings.set_profile(group_id, field, name)
            except ValueError as error:
                return str(error)
            except (OSError, UnicodeError):
                return "群设定保存或读取失败，未完成切换，请检查本地配置文件。"
            return f"本群{label}已切换为{name}，后续请求生效；其他群和私聊不受影响。"
        if group_id < 0 and command == "/清空" and args:
            return "私聊仅支持 /清空 清除自己的私聊记录；群内操作请在对应群执行。"
        if command == "/积极性":
            if group_id <= 0 or self.group_settings is None:
                return "请在要调整的群中使用 /积极性 [0至100]。"
            if not args:
                activity = self.group_settings.get(group_id)["activity"]
                return f"本群积极性：{activity}（0关闭主动回复，@仍有效）。"
            if await self.level(user_id, lookup) < 1:
                return "权限不足，仅机器人所有者和群主、群管理员可调整积极性。"
            if (
                len(args) != 1
                or not args[0].isascii()
                or not args[0].isdigit()
                or not 0 <= int(args[0]) <= 100
            ):
                return "用法：/积极性 0至100，例如 /积极性 50。"
            self.group_settings.set_activity(group_id, int(args[0]))
            return f"本群积极性已设为 {int(args[0])}，立即生效；@机器人仍有效。"
        if command == "/ping" and not args:
            return "pong"
        if command == "/记忆" and not args:
            archive = self.memory.archive
            if archive is None:
                return "长期记忆未启用。"
            facts = archive.facts(key)
            lines = [f"已保存你在本群的 {archive.count(key)} 条消息。认知仅代表用户自述："]
            lines.extend(f"{f['field']}：{f['value']}" for f in facts)
            if not facts:
                lines.append("尚未形成长期认知。")
            return "\n".join(lines)
        if command not in ("/帮助", "/help", "/清空", "/清空全部", "/确认", "/记住", "/忘记"):
            return "未知指令或参数错误。请发送 /帮助 查看可用指令。"
        level = await self.level(user_id, lookup)
        if command in ("/帮助", "/help"):
            if args:
                return "用法：/帮助"
            result = (
                "@我直接说话即可聊天、分析或评论，联网查询由模型决定。"
                "\n/积极性 查看本群参与积极性"
                "\n/ping 测试连接\n/帮助（/help）查看说明\n/记忆 查看自己在本群的长期认知"
            )
            if level >= 1:
                result += (
                    "\n/积极性 0至100 调整本群主动参与程度"
                    "\n/人格 列表、/风格 列表 查看本群设定及切换方式（仅群聊）"
                    "\n/清空 清除自己的会话\n/清空 @某人 清除其本群会话"
                    "\n/清空 本群 清除本群会话（需确认）\n/确认 确认码 执行待确认操作"
                )
                result += "\n/记住 字段 内容 修正本人的认知\n/忘记 字段 删除本人的一项认知"
            if level == 2:
                result += "\n/清空全部 清除本机器人所有群及私聊的会话（需确认）"
            return result
        if level < 1 or (command == "/清空全部" and level < 2):
            return "权限不足，未执行。管理指令仅限群主、群管理员或所有者；清空全部仅限所有者。"
        if command in ("/记住", "/忘记"):
            if self.memory.archive is None:
                return "长期记忆未启用。"
            values = text.split(maxsplit=2)
            if (command == "/记住" and len(values) != 3) or (
                command == "/忘记" and len(values) != 2
            ):
                return "用法：/记住 字段 内容，或 /忘记 字段；只作用于你自己在本群的认知。"
            try:
                async with self.memory.locked(key):
                    if command == "/记住":
                        self.memory.archive.remember(key, values[1], values[2])
                    else:
                        self.memory.archive.forget(key, values[1])
            except ValueError as error:
                return str(error)
            return "认知已更新。历史消息仍保留；彻底删除需使用 /清空。"
        if command == "/确认":
            pending = self.pending.get(key)
            if len(args) != 1 or pending is None or pending.expires <= time.monotonic():
                return "没有有效的待确认操作，或确认已过期。请重新发起清空。"
            if not args[0].isascii() or not secrets.compare_digest(args[0], pending.token):
                return "确认码不正确，未执行。"
            if pending.scope == "all" and level < 2:
                return "权限不足，未执行。清空全部仅限机器人主人。"
            # Consume before awaiting: duplicate/concurrent confirmations cannot replay it.
            del self.pending[key]
            await self.memory.clear_scope(bot_id, None if pending.scope == "all" else group_id)
            return (
                "已清空本机器人所有群及私聊的会话、历史消息和长期认知。"
                if pending.scope == "all"
                else "已清空本群所有会话、历史消息和长期认知。"
            )
        if command == "/清空全部" and args:
            return "用法：/清空全部"
        if command == "/清空全部" or args == ["本群"]:
            scope = "all" if command == "/清空全部" else "group"
            token = secrets.token_hex(4)
            self.pending[key] = Confirmation(token, scope, time.monotonic() + 60)
            label = "本机器人所有群及私聊" if scope == "all" else "本群所有成员"
            return (
                f"将永久删除{label}的会话、历史消息和长期认知。"
                f"请在60秒内由你在本群发送 @机器人 /确认 {token}。"
            )
        target = user_id
        if args:
            if (
                len(args) != 1
                or not args[0].startswith("@")
                or not args[0][1:].isascii()
                or not args[0][1:].isdigit()
                or int(args[0][1:]) <= 0
            ):
                return "用法：/清空、/清空 @某人、/清空 本群"
            target = int(args[0][1:])
        async with self.memory.locked((bot_id, group_id, target)):
            self.memory.clear((bot_id, group_id, target))
        return (
            "已清空你在本群的会话、历史消息和长期认知。"
            if target == user_id
            else f"已清空成员 {target} 在本群的会话、历史消息和长期认知。"
        )

    def cleanup(self):
        now = time.monotonic()
        for key, pending in list(self.pending.items()):
            if pending.expires <= now:
                del self.pending[key]
