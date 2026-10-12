"""验证文字呼叫的边界、正文清理和群名片隔离。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant import addressed, extract_text
from plugins.assistant.config import Config
from plugins.assistant.contacts import Contacts


class TextMentionTests(unittest.IsolatedAsyncioTestCase):
    def test_matching_and_command_body(self):
        bot, config = SimpleNamespace(self_id="99"), Config()
        names = {"机器人", "群名片", "A+B"}
        for prefix in ("@99", "@机器人", "＠群名片", "@A+B"):
            event = SimpleNamespace(
                user_id=1, group_id=10, original_message=Message(prefix + " /任务")
            )
            self.assertTrue(addressed(bot, event, config, names))
            self.assertEqual(extract_text(bot, event, names).strip(), "/任务")
        for text in ("@", "＠", "@别人", "@999", "@机器人甲", "mail@99.com", "a@机器人.cn", "@@99"):
            event = SimpleNamespace(user_id=1, group_id=10, original_message=Message(text))
            self.assertFalse(addressed(bot, event, config, names), text)
            self.assertEqual(extract_text(bot, event, names), text)
        event.original_message = Message("@机器人")
        self.assertEqual(extract_text(bot, event, names), "")
        event.user_id = 99
        self.assertFalse(addressed(bot, event, config, names))

    def test_native_mentions_and_private_text(self):
        bot = SimpleNamespace(self_id="99")
        event = SimpleNamespace(
            user_id=1,
            group_id=10,
            original_message=(MessageSegment.at(99) + Message(" /清空 ") + MessageSegment.at(3)),
        )
        self.assertTrue(addressed(bot, event, Config()))
        self.assertEqual(extract_text(bot, event).split(), ["/清空", "@3"])
        event.group_id = -1
        event.original_message = Message("@99")
        self.assertEqual(extract_text(bot, event), "@99")

    async def test_platform_names_are_group_scoped(self):
        contacts = Contacts(Config())
        bot = SimpleNamespace(
            self_id="99",
            get_group_member_list=AsyncMock(
                return_value=[
                    {"user_id": 99, "nickname": "平台昵称", "card": "甲群名片"},
                ]
            ),
        )
        await contacts.refresh_group(bot, 10)
        contacts.names[(99, 11, 99)] = "乙群名片"
        self.assertEqual(contacts.bot_names(99, 10), {"平台昵称", "甲群名片"})
        self.assertEqual(contacts.bot_names(100, 10), set())
