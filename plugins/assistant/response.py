"""主 agent 的响应选择；发送边界由群聊适配器执行。"""

from .prompt_store import tool_definition

FUNCTION = tool_definition("set_response")


class Response:
    def __init__(self, quotes, repeat=None, topic="", active=False):
        self.quotes = quotes
        self.repeat = repeat
        self.state = {"action": "reply", "quote_message_id": None, "topic": topic, "active": active}

    async def execute(self, args):
        if (
            not isinstance(args, dict)
            or set(args) != {"action", "quote_message_id", "topic", "active"}
            or args["action"] not in ("reply", "silent", "repeat")
            or not isinstance(args["topic"], str)
            or len(args["topic"]) > 200
            or type(args["active"]) is not bool
        ):
            raise ValueError("响应选择无效")
        quote = args["quote_message_id"]
        if quote is not None and (type(quote) is not int or quote not in self.quotes):
            return {"status": "invalid_quote"}
        if args["action"] == "repeat" and not self.repeat:
            return {"status": "repeat_not_available"}
        self.state.update(args)
        return {"status": "selected", **self.state}

    def apply(self, reply):
        if self.state["action"] == "silent":
            reply.text, reply.emote_id, reply.image_data = "", None, None
            reply.media = []
            reply.save = reply.poke_target is not None
        elif self.state["action"] == "repeat":
            reply.text, reply.emote_id, reply.image_data = self.repeat, None, None
            reply.media = []
            reply.save = True
        return reply
