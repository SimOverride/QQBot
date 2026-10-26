"""按当前昵称呈现认知与模型上下文，不改写归档和结构化身份。"""

import json
import re

from .knowledge import read_directory
from .prompt_store import prompt_text

MESSAGE_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_群])(?:消息\s*(?:ids?|编号)|message_ids?|ids?)"
    r"\s*[:：=#]?\s*(?P<numbers>\d{1,19}(?:\s*[-~～至]\s*\d{1,19})?"
    r"(?:\s*[,，、;；]\s*\d{1,19}(?:\s*[-~～至]\s*\d{1,19})?)*)"
    r"(?![A-Za-z0-9_.])",
    re.I,
)


def reference_ids(value):
    """展开有限的消息编号列表和范围，不把长范围变成无界查询。"""
    result = []
    for part in re.split(r"[,，、;；]", value):
        bounds = [int(n) for n in re.split(r"[-~～至]", part)]
        start, end = bounds[0], bounds[-1]
        if not 0 < start <= end < 2**63 or end - start >= 100:
            return []
        result.extend(range(start, end + 1))
        if len(result) > 100:
            return []
    return list(dict.fromkeys(result))


class Names:
    def __init__(self, root, bot, fallback=None):
        self.root = root
        self.bot = int(bot)
        directory = read_directory(root).get(str(bot), {})
        users = directory.get("users", {})
        self.groups = {
            int(g): str(item["name"]).strip()[:100]
            for g, item in directory.get("groups", {}).items()
            if g.isdigit() and item.get("name")
        }
        self.names = {
            int(qq): str(name).strip()[:80]
            for qq, name in (fallback or {}).items()
            if str(qq).isdigit() and name
        }
        self.names.update(
            {
                int(qq): str(item["name"]).strip()[:80]
                for qq, item in users.items()
                if qq.isdigit() and item.get("name")
            }
        )
        self.labels = dict(self.names)
        self.labels[self.bot] = self.labels.get(self.bot, f"QQ {self.bot}") + "（机器人自己）"
        self.named = {label: "person" for label in self.labels.values()}
        self.named.update({label: "group" for label in self.groups.values()})
        labels = "|".join(
            r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])"
            for v in sorted(self.named, key=len, reverse=True)
            if not v.isdigit()
        )
        ids = "|".join(
            map(
                str,
                sorted(
                    set(self.labels) | set(self.groups), key=lambda n: len(str(n)), reverse=True
                ),
            )
        )
        self.pattern = re.compile(
            r"(?<![A-Za-z0-9_.])(?:"
            r"(?:群(?:聊)?(?:号|ID)?|group_id|group|grp|scope)\s*[:：=]?\s*(?P<group>-?\d{1,12})"
            r"|(?:用户\s*QQ(?:号)?|QQ(?:号码|用户|号)?|用户|user_id|usr|sender|bot_qq|bot|related_user)\s*[:：=]?\s*(?P<person>\d{1,12})"
            r"|(?P<bare>" + ids + r"))(?![A-Za-z0-9_.])" + r"|(?P<label>" + labels + r")",
            re.I,
        )

    def _name_parts(self, value):
        """返回纯文本片段；名称标记供后台安全创建高亮节点。"""
        result = []
        for index, chunk in enumerate(re.split(r"((?:https?://|data:)[^\s<>\"']+)", value)):
            if index % 2:
                result.append({"text": chunk})
                continue
            start = 0
            for match in self.pattern.finditer(chunk):
                if match.start() > start:
                    result.append({"text": chunk[start : match.start()]})
                label, kind = match[0], None
                if match["label"]:
                    kind = self.named.get(match[0])
                elif match["person"]:
                    identifier = int(match["person"])
                    if identifier in self.labels:
                        label, kind = self.labels[identifier], "person"
                elif match["group"]:
                    identifier = int(match["group"])
                    if identifier in self.groups:
                        label, kind = self.groups[identifier], "group"
                    elif identifier < 0 and -identifier in self.labels:
                        label, kind = "私聊（" + self.labels[-identifier] + "）", "person"
                else:
                    identifier = int(match["bare"])
                    # 人和群恰好同号时，裸数字不能猜测归属。
                    if identifier in self.labels and identifier not in self.groups:
                        label, kind = self.labels[identifier], "person"
                    elif identifier in self.groups and identifier not in self.labels:
                        label, kind = self.groups[identifier], "group"
                result.append({"text": label, **({"kind": kind} if kind else {})})
                start = match.end()
            if start < len(chunk):
                result.append({"text": chunk[start:]})
        return result

    def parts(self, value):
        """消息引用先整体识别，编号内部不参与昵称匹配。"""
        result = []
        for index, chunk in enumerate(re.split(r"((?:https?://|data:)[^\s<>\"']+)", value)):
            if index % 2:
                result.append({"text": chunk})
                continue
            start = 0
            for match in MESSAGE_REFERENCE.finditer(chunk):
                result.extend(self._name_parts(chunk[start : match.start()]))
                result.append(
                    {"text": match[0], "kind": "message", "ids": reference_ids(match["numbers"])}
                )
                start = match.end()
            result.extend(self._name_parts(chunk[start:]))
        return result

    def plain(self, value):
        return "".join(part["text"] for part in self.parts(value))

    def fragments(self, value):
        if isinstance(value, str):
            return self.parts(value)
        if isinstance(value, dict):
            return {k: self.fragments(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.fragments(v) for v in value]
        return value

    def text(self, value):
        """混合说明文字与 JSON 时，只替换文字，不破坏 JSON 数字和键。"""
        result, start, cursor = [], 0, 0
        decoder = json.JSONDecoder()
        while cursor < len(value):
            if value[cursor] not in "[{":
                cursor += 1
                continue
            try:
                data, end = decoder.raw_decode(value[cursor:])
            except ValueError:
                cursor += 1
                continue
            if isinstance(data, list) and not any(isinstance(v, (dict, list)) for v in data):
                cursor += end
                continue
            result.append(self.plain(value[start:cursor]))
            result.append(json.dumps(self.data(data), ensure_ascii=False))
            cursor += end
            start = cursor
        result.append(self.plain(value[start:]))
        return "".join(result)

    def data(self, value, key=""):
        # 字符串类型的协议编号、昵称与图片地址也保持原样。
        protected = {
            "id",
            "qq",
            "bot_qq",
            "usr",
            "sender",
            "subject_qq",
            "group_id",
            "scope",
            "message_id",
            "source_id",
            "related_user",
            "name",
            "sender_name",
            "sender_nickname",
            "image_url",
            "url",
            "call_id",
            "tool_call_id",
            "arguments",
            "encrypted_content",
            "platform_display_names",
            "mentions",
            "person_qq",
        }
        if key in protected:
            return value
        if isinstance(value, str):
            if key in {
                "总体认知",
                "会话印象",
                "角色",
                "互动习惯",
                "互动关系",
                "补充认知",
                "summary",
                "knowledge",
                "value",
                "evidence",
                "text",
            }:
                return self.plain(value)
            return self.text(value)
        if isinstance(value, list):
            return [self.data(v) for v in value]
        if isinstance(value, dict):
            result = {
                k: self.data(v, k)
                for k, v in value.items()
                if k not in {"current_names", "platform_display_names"}
            }
            qq = value.get("qq", value.get("sender", value.get("usr")))
            if isinstance(qq, int) and qq in self.names:
                for field in ("sender_name", "sender_nickname"):
                    if field in result:
                        result[field] = self.names[qq]
            return result
        return value

    def instructions(self):
        return prompt_text("history.names", root=self.root) + json.dumps(
            {"bot_self": {"qq": self.bot, "name": self.names.get(self.bot), "is_self": True}},
            ensure_ascii=False,
        )

    def messages(self, messages):
        """每轮从原始消息生成副本，不自动附加人物或群聊对应表。"""
        result = []
        for message in messages:
            item = dict(message)
            if "content" in message:
                item["content"] = self.data(message["content"])
            if message.get("type") == "function_call_output":
                item["output"] = self.data(message.get("output", ""))
            result.append(item)
        if result and result[0].get("role") == "system":
            result[0]["content"] += self.instructions()
        return result


def for_request(config, history_tools=None, bot=None, identity=""):
    from . import prompt_store

    root = prompt_store.ROOT
    if history_tools is not None:
        root = getattr(getattr(history_tools, "archive", None), "root", root)
        bot = history_tools.key[0]
    if bot is None:
        match = re.search(r'"bot_qq"\s*:\s*(\d+)', identity)
        if not match:
            return None
        bot = int(match[1])
    return Names(root, bot, config.user_names)
