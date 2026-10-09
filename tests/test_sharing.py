import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.service import Assistant
from plugins.assistant.sharing import Sharing


class SharingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.archive = LongTermMemory(Path(self.temp.name) / "memory.db", self.cfg)
        self.llm = SimpleNamespace(
            screen_disclosure=AsyncMock(return_value={"safe": True, "group_id": 10})
        )
        self.service = Assistant(self.cfg, self.llm, archive=self.archive)
        self.contacts = SimpleNamespace(
            for_scope=lambda *args: {}, private_access=AsyncMock(return_value=(True, [10]))
        )
        self.sharing = Sharing(self.service, self.contacts)
        self.bot = SimpleNamespace(
            self_id="99", send_group_forward_msg=AsyncMock(return_value={"message_id": 900})
        )

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def message(self, mid=1, text="我喜欢研究天文摄影", old=False):
        key = (99, -2, 2)
        self.archive.collect(key, mid, text, ["text"], time.time() - (999 if old else 0))
        self.archive.save(key, mid, text, "好的")
        return self.archive.db.execute(
            "SELECT id FROM messages WHERE bot=99 AND grp=-2 AND message_id=?", (mid,)
        ).fetchone()[0]

    async def test_share_and_reuse_without_cross_person_leak(self):
        source = self.message()
        self.archive.update_facts(
            (99, -2, 2),
            source,
            "我喜欢研究天文摄影",
            [{"field": "总体认知", "value": "天文摄影", "evidence": "喜欢研究天文摄影"}],
        )
        self.assertEqual(self.archive.facts((99, 10, 2)), [])
        await self.sharing.review(self.bot, source)
        self.bot.send_group_forward_msg.assert_awaited_once()
        self.assertIn("天文摄影", self.archive.context((99, 10, 2), "兴趣", set()))
        self.assertEqual(self.archive.facts((99, 10, 2))[0]["value"], "天文摄影")
        self.assertEqual(self.archive.shared_knowledge((99, 10, 3)), [])
        await self.sharing.review(self.bot, source)
        self.bot.send_group_forward_msg.assert_awaited_once()
        self.archive.delete(99, -2, 2)
        self.assertEqual(self.archive.shared_knowledge((99, 10, 2)), [])
        self.assertEqual(self.archive.facts((99, 10, 2)), [])

    async def test_sensitive_and_semantic_denial(self):
        await self.sharing.review(self.bot, self.message(text="我的密码是abc，保密"))
        self.llm.screen_disclosure.assert_not_awaited()
        self.bot.send_group_forward_msg.assert_not_awaited()
        self.assertEqual(self.archive.shared_knowledge((99, 10, 2)), [])

    async def test_second_review_fail_closed(self):
        self.llm.screen_disclosure.side_effect = [{"safe": True, "group_id": 10}, {"safe": False}]
        await self.sharing.review(self.bot, self.message())
        self.bot.send_group_forward_msg.assert_not_awaited()
        self.assertEqual(self.archive.shared_knowledge((99, 10, 2)), [])

    async def test_membership_rechecked(self):
        self.contacts.private_access.side_effect = [(True, [10]), (True, [])]
        await self.sharing.review(self.bot, self.message())
        self.bot.send_group_forward_msg.assert_not_awaited()

    async def test_backfill_never_sends(self):
        await self.sharing.review(self.bot, self.message(old=True))
        self.bot.send_group_forward_msg.assert_not_awaited()
        self.assertTrue(self.archive.shared_knowledge((99, 10, 2)))

    async def test_rate_limit_and_failed_send_not_retried(self):
        self.bot.send_group_forward_msg.side_effect = RuntimeError
        source = self.message()
        await self.sharing.review(self.bot, source)
        await self.sharing.review(self.bot, source)
        await self.sharing.review(self.bot, self.message(2, "我喜欢制作木工模型"))
        self.bot.send_group_forward_msg.assert_awaited_once()

    async def test_later_confidentiality_retracts_knowledge(self):
        await self.sharing.review(self.bot, self.message(old=True))
        self.assertTrue(self.archive.shared_knowledge((99, 10, 2)))
        await self.sharing.review(self.bot, self.message(2, "刚才的事情需要保密"))
        self.assertEqual(self.archive.shared_knowledge((99, 10, 2)), [])

    async def test_cleared_during_review_cannot_resurrect(self):
        async def clear(*args):
            self.archive.delete(99, -2, 2)
            return {"safe": True, "group_id": 10}

        self.llm.screen_disclosure.side_effect = clear
        await self.sharing.review(self.bot, self.message())
        self.bot.send_group_forward_msg.assert_not_awaited()
        self.assertEqual(self.archive.shared_knowledge((99, 10, 2)), [])

    async def test_without_saved_reply_and_multiple_text_segments(self):
        self.archive.collect((99, -2, 2), 1, "测试将这条普通消息分享到群里",
                             ["text", "text"], time.time())
        await self.sharing.review(self.bot, 1)
        self.bot.send_group_forward_msg.assert_awaited_once()
        payload = self.bot.send_group_forward_msg.call_args.kwargs
        self.assertEqual(payload["group_id"], 10)
        self.assertEqual(payload["messages"][0]["data"]["uin"], "2")
        self.assertEqual(payload["messages"][0]["data"]["content"][0]["data"]["text"],
                         "测试将这条普通消息分享到群里")
