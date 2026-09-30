import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.config import Config
from plugins.assistant.limits import Limits
from plugins.assistant.llm import LLM
from plugins.assistant.notices import Notices
from plugins.assistant.personality import ProfileStore


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tokens_and_rows_are_preserved(self):
        llm = SimpleNamespace(
            rephrase_notice=AsyncMock(return_value="请用 /确认 deadbeef，60秒内哦。")
        )
        notices = Notices(llm, ProfileStore(), Limits(Config()))
        source = "请在60秒内使用 /确认 deadbeef。"
        self.assertIn("deadbeef", await notices.render(source))
        llm.rephrase_notice.return_value = "已经完成了。"
        self.assertEqual(await notices.render(source + "未执行。"), source + "未执行。")
        llm.rephrase_notice.return_value = "这是你的资料呀："
        output = await notices.render("你的资料：\n兴趣：天文\n称呼：小明")
        self.assertTrue(output.endswith("\n兴趣：天文\n称呼：小明"))

    async def test_failure_falls_back_and_cache_respects_style(self):
        llm = SimpleNamespace(rephrase_notice=AsyncMock(side_effect=RuntimeError))
        notices = Notices(llm, ProfileStore(), Limits(Config()))
        self.assertEqual(await notices.render("未执行。"), "未执行。")
        llm.rephrase_notice.side_effect = None
        llm.rephrase_notice.return_value = "还没执行哦。"
        self.assertEqual(await notices.render("未执行。"), "还没执行哦。")
        await notices.render("未执行。")
        self.assertEqual(llm.rephrase_notice.await_count, 2)

    async def test_semantic_rejection_keeps_program_result(self):
        llm = LLM(None, Config(), SimpleNamespace(enabled=False))
        llm.step = AsyncMock(side_effect=[("已清空全部！", [], []), ("NO", [], [])])
        source = "权限不足，未执行。"
        self.assertEqual(await llm.rephrase_notice(source, {}), source)
        self.assertTrue(all(call.args[1] is False for call in llm.step.call_args_list))

    async def test_overload_does_not_add_model_load(self):
        llm = SimpleNamespace(rephrase_notice=AsyncMock())
        limits = Limits(Config(llm_max_concurrency=1))
        notices = Notices(llm, ProfileStore(), limits)
        async with limits.slot():
            self.assertEqual(await notices.render("当前请求较多。"), "当前请求较多。")
        llm.rephrase_notice.assert_not_awaited()
