"""平台目录同步与消息昵称更新，不访问真实 QQ。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.contacts import Contacts
from plugins.assistant.longterm import LongTermMemory


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_names_members_and_event_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            contacts = Contacts(Config())
            contacts.directory_path = Path(directory) / "contacts.json"
            bot = SimpleNamespace(
                self_id="99",
                get_login_info=AsyncMock(return_value={"user_id": 99, "nickname": "机器人昵称"}),
                get_group_list=AsyncMock(return_value=[{"group_id": 10, "group_name": "同好群"}]),
                get_group_member_list=AsyncMock(
                    return_value=[{"user_id": 2, "nickname": "QQ昵称", "card": "群内别名"}]
                ),
                get_friend_list=AsyncMock(return_value=[]),
            )
            await contacts.sync_directory(bot)
            saved = json.loads(contacts.directory_path.read_text(encoding="utf-8"))["99"]
            self.assertEqual(saved["users"]["2"]["name"], "QQ昵称")
            self.assertEqual(saved["groups"]["10"], {"name": "同好群", "members": [2]})
            await contacts.sync_directory(bot)
            bot.get_group_list.assert_awaited_once()
            contacts.observe(99, 10, 2, SimpleNamespace(nickname="新昵称", card="名片"))
            saved = json.loads(contacts.directory_path.read_text(encoding="utf-8"))["99"]
            self.assertEqual(saved["users"]["2"]["name"], "新昵称")
            contacts.directory_refreshed.clear()
            bot.get_group_list.side_effect = RuntimeError()
            await contacts.sync_directory(bot)
            self.assertEqual(contacts.directory["99"]["groups"]["10"]["name"], "同好群")

    async def test_missing_historical_user_and_retry_without_waiting_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = LongTermMemory(root / "memory.sqlite3", Config())
            archive.collect((99, 10, 22), 1, "测试", ["text"], 100)
            archive.close()
            contacts = Contacts(Config())
            contacts.directory_path = root / "contacts.json"
            bot = SimpleNamespace(
                self_id="99",
                get_login_info=AsyncMock(return_value={"user_id": 99, "nickname": "机器人昵称"}),
                get_group_list=AsyncMock(
                    side_effect=[RuntimeError(), [{"group_id": 10, "group_name": "真实群名"}]]
                ),
                get_friend_list=AsyncMock(return_value=[]),
                get_group_member_list=AsyncMock(return_value=[]),
                get_stranger_info=AsyncMock(return_value={"user_id": 22, "nickname": "当前昵称"}),
            )
            await contacts.sync_directory(bot)
            self.assertNotIn(99, contacts.directory_refreshed)
            await contacts.sync_directory(bot)
            self.assertEqual(bot.get_group_list.await_count, 2)
            self.assertEqual(contacts.directory["99"]["users"]["22"]["name"], "当前昵称")
            self.assertEqual(contacts.directory["99"]["groups"]["10"]["name"], "真实群名")
            bot.get_stranger_info.assert_awaited_with(user_id=22, no_cache=True)
            await contacts.sync_directory(bot, force=True)
            self.assertEqual(bot.get_group_list.await_count, 3)

    async def test_group_names_published_before_member_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            contacts = Contacts(Config())
            contacts.directory_path = Path(directory) / "contacts.json"

            published = []

            async def members(**kwargs):
                saved = json.loads(contacts.directory_path.read_text(encoding="utf-8"))
                published.append(saved["99"]["groups"]["11"]["name"])
                raise RuntimeError("成员接口不可用")

            bot = SimpleNamespace(
                self_id="99",
                get_login_info=AsyncMock(return_value={"user_id": 99, "nickname": "机器人昵称"}),
                get_group_list=AsyncMock(
                    return_value=[
                        {"group_id": 10, "group_name": "第一群"},
                        {"group_id": 11, "group_name": "第二群"},
                    ]
                ),
                get_friend_list=AsyncMock(return_value=[]),
                get_group_member_list=AsyncMock(side_effect=members),
            )
            await contacts.sync_directory(bot)
            self.assertEqual(bot.get_group_member_list.await_count, 2)
            self.assertEqual(published, ["第二群", "第二群"])
            self.assertNotIn(99, contacts.directory_refreshed)
