import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.llm import Reply
from plugins.assistant.memory import Memory
from plugins.assistant.personality import ProfileStore
from plugins.assistant.prompts import system_prompt
from plugins.assistant.service import Assistant


class PersonalityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_remote_configuration_even_for_owner(self):
        config = Config(bot_owners={1})
        commands = Commands(config, Memory(config))
        for command in ("/人设", "/人设 新人设", "/全局人设", "/风格", "/风格 温柔", "/全局风格"):
            self.assertIn("未知", await commands.run((99, 10, 1), command, None))
        help_text = await commands.run((99, 10, 1), "/帮助", None)
        self.assertNotIn("人设", help_text)
        self.assertNotIn("风格", help_text)

    async def test_local_hot_reload_and_request_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            persona, style = Path(directory) / "persona.txt", Path(directory) / "style.txt"
            persona.write_text("LOCAL_PERSONA", encoding="utf8")
            style.write_text("LOCAL_STYLE", encoding="utf8")
            store = ProfileStore(persona, style)
            self.assertEqual(store.effective(),
                             {"persona": "LOCAL_PERSONA", "style": "LOCAL_STYLE"})
            llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("回答")))
            service = Assistant(Config(), llm, store)
            await service.handle(99, 10, 1, 1, "你好", AsyncMock())
            self.assertEqual(llm.reply.call_args.args[3], store.effective())
            style.write_text("NEW_STYLE", encoding="utf8")
            persona.write_text("NEW_PERSONA", encoding="utf8")
            await service.handle(99, 10, 2, 2, "你好", AsyncMock())
            self.assertEqual(llm.reply.call_args.args[3]["style"], "NEW_STYLE")
            self.assertEqual(llm.reply.call_args.args[3]["persona"], "NEW_PERSONA")
            prompt = system_prompt(False, store.effective())
            self.assertIn("NEW_PERSONA", prompt)
            self.assertIn("NEW_STYLE", prompt)
            self.assertIn("当前搜索未配置", prompt)
            self.assertIn("不要在聊天中复述", prompt)

    async def test_invalid_files_do_not_leak_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "persona.txt"
            with self.assertRaises(ValueError):
                ProfileStore(path)
            for value in ("", "PRIVATE" * 1000):
                path.write_text(value, encoding="utf8")
                with self.assertRaises(ValueError) as error:
                    ProfileStore(path)
                self.assertNotIn("PRIVATE", str(error.exception))
            path.write_bytes(bytes([255, 254, 253]))
            with self.assertRaises(ValueError):
                ProfileStore(path)
