import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.contacts import Contacts
from plugins.assistant.identity import identity_context
from plugins.assistant.llm import Reply
from plugins.assistant.service import Assistant


class PrivateTests(unittest.IsolatedAsyncioTestCase):
    async def test_contact_access(self):
        contacts = Contacts(Config())
        bot = SimpleNamespace(self_id="99", get_friend_list=AsyncMock(return_value=[]),
                              get_group_list=AsyncMock(return_value=[{"group_id": 10}]),
                              get_group_member_info=AsyncMock(side_effect=RuntimeError))
        self.assertEqual(await contacts.private_access(bot, 2), (False, []))
        bot.get_friend_list.return_value = [{"user_id": 2, "nickname": "好友"}]
        self.assertEqual(await contacts.private_access(bot, 2), (True, []))
        bot.get_friend_list.return_value = []
        bot.get_group_member_info.side_effect = None
        bot.get_group_member_info.return_value = {"user_id": 2, "group_id": 10, "card": "群友"}
        self.assertEqual(await contacts.private_access(bot, 2), (True, [10]))
        bot.get_group_member_info.return_value = {"user_id": 3, "group_id": 10}
        self.assertEqual(await contacts.private_access(bot, 2), (False, []))
        bot.get_friend_list.side_effect = RuntimeError
        bot.get_group_member_info.side_effect = RuntimeError
        self.assertEqual(await contacts.private_access(bot, 2), (False, []))

    async def test_private_scope_and_authorization(self):
        cfg = Config(bot_owners={1}, cooldown_seconds=0)
        llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("ok")))
        service = Assistant(cfg, llm)
        send = AsyncMock()
        await service.handle(99, -2, 2, 1, "hello", send)
        llm.reply.assert_not_awaited()
        await service.handle(99, -3, 2, 1, "hello", send, private_allowed=True)
        llm.reply.assert_not_awaited()
        await service.handle(99, -2, 2, 1, "hello", send, private_allowed=True)
        llm.reply.assert_awaited_once()
        self.assertEqual(service.memory.history((99, 10, 2)), [])
        self.assertEqual(service.memory.history((99, -3, 3)), [])
        result = await service.commands.run((99, -1, 1), "/清空 @2", None)
        self.assertIn("仅支持", result)
        result = await service.commands.run((99, -1, 1), "/清空 本群", None)
        self.assertIn("仅支持", result)

    async def test_nickname_is_display_only(self):
        contacts = Contacts(Config())
        contacts.observe(99, 10, 2, SimpleNamespace(card="群名片", nickname="昵称"))
        self.assertEqual(contacts.for_scope(99, 10), {2: "群名片"})
        self.assertEqual(contacts.for_scope(99, -2), {})
        context = identity_context(Config(bot_owners={1}), 99, 10, 2, {2: "机器人主人"})
        self.assertIn('"role": "普通用户"', context)
