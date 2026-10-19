"""聊天模块目录：按当前会话发现能力，并复用各模块的权限入口。"""

CAPABILITIES = {
    "name": "read_capabilities",
    "description": "查询本会话实际可用工具和群插件开关，不扩大权限。",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}
PLUGIN = {
    "name": "manage_plugin",
    "description": "查询或按明确委托开关插件。本群需管理员，全局需机器人主人，不能扩大插件范围。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "enable", "disable"]},
            "plugin": {"type": "string", "description": "插件ID，查询列表时为空"},
            "scope": {"type": "string", "enum": ["group", "global"]},
        },
        "required": ["action", "plugin", "scope"],
        "additionalProperties": False,
    },
}


class Modules:
    def __init__(self, session):
        self.response = getattr(session, "response", None)
        self.tasks = getattr(session, "tasks", None)
        self.extensions = getattr(session, "extensions", None)

    def functions(self):
        result = [CAPABILITIES]
        if self.response:
            from .response import FUNCTION

            result.append(FUNCTION)
        if self.tasks:
            result.extend(self.tasks.functions())
        if self.extensions:
            result.append(PLUGIN)
            result.extend(self.extensions.functions())
        return result

    async def execute(self, name, args, available):
        if name == "set_response" and self.response:
            return await self.response.execute(args)
        if name == "read_capabilities":
            if args != {}:
                raise ValueError
            plugins = []
            if self.extensions:
                session = self.extensions
                bot, group, _ = session.key
                for ident, plugin in session.manager.plugins.items():
                    if session.manager.visible(bot, group, ident):
                        plugins.append(
                            {
                                "id": ident,
                                "title": plugin.title,
                                "enabled": session.manager.enabled(bot, group, ident),
                            }
                        )
            return {
                "tools": [{"name": f["name"], "description": f["description"]} for f in available],
                "plugins": plugins,
                "hint": "可见不代表有操作权限；任务审批、插件确认及发送校验仍由各模块执行。",
            }
        if name == "manage_plugin" and self.extensions:
            if (
                not isinstance(args, dict)
                or set(args) != {"action", "plugin", "scope"}
                or args["action"] not in ("list", "enable", "disable")
                or args["scope"] not in ("group", "global")
                or not isinstance(args["plugin"], str)
            ):
                raise ValueError
            if args["action"] == "list":
                if args["plugin"]:
                    raise ValueError
                command = "/插件"
            else:
                if args["plugin"] not in self.extensions.manager.plugins:
                    return {"status": "not_found"}
                scope = "本群" if args["scope"] == "group" else "全局"
                action = "开启" if args["action"] == "enable" else "关闭"
                command = f"/插件 {scope} {args['plugin']} {action}"
            result = await self.extensions.manager.command(
                self.extensions.key,
                command,
                self.extensions.lookup,
            )
            return {"result": result, "hint": "以实际返回文字为准，拒绝不表示修改成功。"}
        if self.tasks and name in {f["name"] for f in self.tasks.functions()}:
            return await self.tasks.execute(name, args)
        if self.extensions:
            return await self.extensions.execute(name, args)
        return {"status": "unsupported_tool"}
