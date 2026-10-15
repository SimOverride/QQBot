"""纯文字复读候选及防循环规则。"""

import unittest
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.assistant.repeat import RepeatState


def event(user, mid, text):
    return SimpleNamespace(user_id=user, message_id=mid, original_message=Message(text))


class RepeatTests(unittest.TestCase):
    def test_distinct_users_exact_text_and_window(self):
        state = RepeatState()
        state.observe(event(1, 1, "好耶！\n"), False, 0)
        state.observe(event(1, 2, "好耶！\n"), False, 1)
        self.assertIsNone(state.candidate(2, 1))
        state.observe(event(2, 3, "好耶！\n"), False, 2)
        self.assertEqual(state.candidate(3, 2), "好耶！\n")
        self.assertIsNone(state.candidate(2, 2))
        self.assertIsNone(state.candidate(3, 121))
        state.observe(event(3, 4, "好耶！"), False, 3)
        self.assertIsNone(state.candidate(4, 3))

    def test_interruption_explicit_commands_and_media(self):
        for middle, explicit in (
            ("别的话", False),
            ("同文", True),
            ("/帮助", False),
            (MessageSegment.image("https://example.com/a.png"), False),
            (MessageSegment.reply(1) + MessageSegment.text("同文"), False),
        ):
            state = RepeatState()
            state.observe(event(1, 1, "同文"), False, 0)
            state.observe(event(2, 2, middle), explicit, 1)
            state.observe(event(3, 3, "同文"), False, 2)
            self.assertIsNone(state.candidate(3, 2))

    def test_attempt_cooldown(self):
        from unittest.mock import patch

        state = RepeatState()
        with patch("plugins.assistant.repeat.time.monotonic", return_value=100):
            state.reserve("同文")
        state.observe(event(1, 1, "同文"), False, 1800)
        state.observe(event(2, 2, "同文"), False, 1801)
        self.assertIsNone(state.candidate(2, 1801))
        self.assertEqual(state.candidate(2, 1900), "同文")
