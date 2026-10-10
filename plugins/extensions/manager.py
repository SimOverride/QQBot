"""可信本地插件注册、分级管理及请求范围绑定。"""

import asyncio
import time

from plugins.assistant.trace import emit

from .store import Store


class Session:
    def __init__(self, manager, key, lookup):
        self.manager, self.key, self.lookup = manager, key, lookup

    def functions(self):
        return [
            f
            for p in self.manager.plugins.values()
            if self.manager.enabled(*self.key[:2], p.id)
            for f in p.functions
        ]

    async def execute(self, name, args):
        for plugin in self.manager.plugins.values():
            if name in {f["name"] for f in plugin.functions}:
                if not self.manager.enabled(*self.key[:2], plugin.id):
                    return {"status": "plugin_disabled"}
                if await self.manager.commands.level(self.key[2], self.lookup) < 1:
                    return {"status": "access_denied", "hint": "仅本群管理者可管理群插件"}
                if not self.manager.enabled(*self.key[:2], plugin.id):
                    return {"status": "plugin_disabled"}
                return await plugin.execute(self, args)
        return {"status": "unsupported_tool"}


class Manager:
    def __init__(self, root, commands, plugins, archive=None):
        self.store, self.commands = Store(root), commands
        self.archive = archive
        self.config = commands.config
        self.plugins = {}
        self.pending = {}
        self.started = time.time()
        self.connected = {}
        self.activated = {}
        self.lock = asyncio.Lock()
        for plugin in plugins:
            if plugin.id in self.plugins:
                raise ValueError("插件标识重复")
            self.plugins[plugin.id] = plugin
        names = [f["name"] for p in plugins for f in p.functions]
        if len(names) != len(set(names)):
            raise ValueError("插件工具名称重复")

    def entry(self, bot, name, value=None):
        try:
            value = self.store.read() if value is None else value
        except (ValueError, OSError):
            emit(self.config, "plugin_config_error")
            return None
        return value["bots"].get(str(bot), {}).get(name)

    def visible(self, bot, group, name):
        entry = self.entry(bot, name)
        return bool(
            entry and group > 0 and (entry["scope"] is None or str(group) in entry["scope"])
        )

    def enabled(self, bot, group, name):
        entry = self.entry(bot, name)
        return bool(
            entry
            and group > 0
            and entry["enabled"]
            and (entry["scope"] is None or str(group) in entry["scope"])
            and entry["groups"].get(str(group), False)
        )

    def session(self, key, lookup):
        return Session(self, key, lookup)

    def is_command(self, command):
        return command == "/插件" or any(command == p.command_name for p in self.plugins.values())

    async def command(self, key, text, lookup):
        bot, group, user = key
        parts = text.split()
        level = await self.commands.level(user, lookup if group > 0 else None)
        if parts[0] != "/插件":
            plugin = next(p for p in self.plugins.values() if p.command_name == parts[0])
            if not self.visible(bot, group, plugin.id):
                return "该插件不适用于当前群；私聊不能管理群插件。"
            if level < 1:
                return "权限不足，仅本群管理员、群主或机器人主人可管理。"
            return await plugin.command(self.session(key, lookup), parts[1:])
        if len(parts) == 1:
            lines = []
            for name, plugin in self.plugins.items():
                entry = self.entry(bot, name)
                if entry and (level == 2 or self.visible(bot, group, name)):
                    state = "启用" if self.enabled(bot, group, name) else "停用"
                    lines.append(
                        f"{plugin.title}（{name}）：全局{'开' if entry['enabled'] else '关'}，"
                        f"本群{state}"
                    )
            return "\n".join(lines) or "当前没有可用插件。"
        if len(parts) != 4 or parts[1] not in ("全局", "本群") or parts[3] not in ("开启", "关闭"):
            return "用法：/插件；/插件 本群 插件ID 开启|关闭；/插件 全局 插件ID 开启|关闭"
        scope, name, action = parts[1:]
        if level < (2 if scope == "全局" else 1):
            return "权限不足；全局仅机器人主人可管理，本群须群管理员以上。"
        if name not in self.plugins or self.entry(bot, name) is None:
            return "插件不存在。"
        if scope == "本群" and not self.visible(bot, group, name):
            return "该插件不适用于当前群，不能扩大使用范围。"
        value = self.store.read()
        entry = self.entry(bot, name, value)
        enabled = action == "开启"
        if scope == "全局":
            entry["enabled"] = enabled
        else:
            entry["groups"][str(group)] = enabled
        self.store.write(value)
        if enabled:
            for target in entry["groups"] if scope == "全局" else [str(group)]:
                self.activated[(bot, int(target), name)] = time.time()
        # 任何开关变化都使旧确认失效，避免停用后恢复旧操作。
        self.pending = {
            k: v
            for k, v in self.pending.items()
            if not (k[0] == bot and v["plugin"] == name and (scope == "全局" or k[1] == group))
        }
        emit(
            self.config,
            "plugin_switch",
            bot_id=bot,
            group_id=group,
            plugin=name,
            scope=scope,
            enabled=enabled,
            actor=user,
        )
        suffix = "；全局仍关闭，本群暂不可用" if scope == "本群" and not entry["enabled"] else ""
        return f"{self.plugins[name].title}{scope}已{action}{suffix}。"

    async def tick(self, bots):
        async with self.lock:
            now = time.time()
            self.connected = {key: value for key, value in self.connected.items() if key in bots}
            for bot in bots:
                self.connected.setdefault(bot, now)
            for plugin in self.plugins.values():
                try:
                    await plugin.tick(self, bots, now)
                except Exception as error:
                    emit(
                        self.config,
                        "plugin_error",
                        plugin=plugin.id,
                        error_type=type(error).__name__,
                    )
