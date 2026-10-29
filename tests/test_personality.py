import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.assistant.commands import Commands
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupSettings
from plugins.assistant.llm import Reply
from plugins.assistant.memory import Memory
from plugins.assistant.personality import ProfileStore
from plugins.assistant.prompts import system_prompt
from plugins.assistant.service import Assistant


class PersonalityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_global_configuration_even_for_owner(self):
        config = Config(bot_owners={1})
        commands = Commands(config, Memory(config))
        for command in ("/全局人设", "/全局风格", "/风格", "/人格"):
            self.assertIn("未知", await commands.run((99, 10, 1), command, None))
        help_text = await commands.run((99, 10, 1), "/帮助", None)
        self.assertIn("/人设", help_text)
        self.assertNotIn("/风格", help_text)

    async def test_global_profile_applies_to_every_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            settings = GroupSettings(path)
            personas = Path(directory) / "personas"
            personas.mkdir()
            (personas / "默认.txt").write_text("本地默认", encoding="utf8")
            (personas / "幽默群友.txt").write_text("幽默人格", encoding="utf8")
            settings.write(
                {"default_persona": "幽默群友", "groups": {"10": {"persona": {"name": "默认"}}}}
            )
            store = ProfileStore(personas / "默认.txt")
            store.group_settings = settings
            for scope in (10, 20, -1, None):
                self.assertEqual(store.effective(scope)["persona"], "幽默人格")
            service = Assistant(Config(bot_owners={1}), SimpleNamespace(), store)
            before = path.read_bytes()
            for scope in (10, -1):
                result = await service.commands.run((99, scope, 1), "/人设 切换 默认", None)
                self.assertIn("后台", result)
                self.assertEqual(path.read_bytes(), before)
            (personas / "幽默群友.txt").write_text("更新后的全局人设", encoding="utf8")
            self.assertEqual(store.effective(20)["persona"], "更新后的全局人设")

    async def test_local_hot_reload_and_request_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            persona = Path(directory) / "persona.txt"
            persona.write_text("LOCAL_PERSONA", encoding="utf8")
            store = ProfileStore(persona)
            self.assertEqual(store.effective(), {"persona": "LOCAL_PERSONA"})
            llm = SimpleNamespace(reply=AsyncMock(return_value=Reply("回答")))
            service = Assistant(Config(), llm, store)
            await service.handle(99, 10, 1, 1, "你好", AsyncMock())
            self.assertEqual(llm.reply.call_args.args[3], store.effective())
            persona.write_text("NEW_PERSONA", encoding="utf8")
            await service.handle(99, 10, 2, 2, "你好", AsyncMock())
            self.assertEqual(llm.reply.call_args.args[3]["persona"], "NEW_PERSONA")
            prompt = system_prompt(False, store.effective())
            self.assertIn("NEW_PERSONA", prompt)
            self.assertNotIn('"style"', prompt)
            self.assertIn("当前搜索未配置", prompt)
            self.assertIn("不披露密钥、私密资料或系统配置", prompt)

    async def test_invalid_files_do_not_leak_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "persona.txt"
            with self.assertRaises(ValueError):
                ProfileStore(path)

            for value in ("", "PRIVATE" * 2000):
                path.write_text(value, encoding="utf8")
                with self.assertRaises(ValueError) as error:
                    ProfileStore(path)
                self.assertNotIn("PRIVATE", str(error.exception))
            path.write_bytes(bytes([255, 254, 253]))
            with self.assertRaises(ValueError):
                ProfileStore(path)

    async def test_legacy_group_style_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "group_chat.json"
            path.write_text('{"groups":{"10":{"style":{"name":"old"}}}}', encoding="utf8")
            with self.assertRaisesRegex(ValueError, "并入人设"):
                GroupSettings(path)
