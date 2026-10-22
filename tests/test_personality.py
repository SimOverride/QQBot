import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    async def test_group_profiles_permissions_persistence_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            settings = GroupSettings(path)
            personas = Path(directory) / "personas"
            personas.mkdir()
            for folder in (personas,):
                (folder / "默认.txt").write_text("本地默认", encoding="utf8")
            (personas / "幽默群友.txt").write_text("幽默人格", encoding="utf8")
            store = ProfileStore(personas / "默认.txt")
            store.group_settings = settings
            llm = SimpleNamespace(
                reply=AsyncMock(return_value=Reply("回答")),
                rephrase_notice=AsyncMock(return_value="操作完成"),
            )
            service = Assistant(Config(bot_owners={1}), llm, store)
            service.commands.group_settings = settings
            commands = service.commands
            original = path.read_text(encoding="utf8")
            for lookup in (
                None,
                AsyncMock(return_value="member"),
                AsyncMock(side_effect=RuntimeError),
            ):
                result = await commands.run((99, 10, 2), "/人设 幽默群友", lookup)
                self.assertIn("权限不足", result)
                self.assertEqual(path.read_text(encoding="utf8"), original)
            for role in ("admin", "owner"):
                result = await commands.run(
                    (99, 10, 2), "/人设 切换 幽默群友", AsyncMock(return_value=role)
                )
                self.assertIn("已切换", result)
            self.assertEqual(store.effective(10)["persona"], "幽默人格")
            self.assertEqual(store.effective(20), store.effective())
            self.assertEqual(store.effective(-1), store.effective())
            store.group_settings = GroupSettings(path)
            await service.handle(99, 10, 3, 1, "你好", AsyncMock())
            self.assertEqual(llm.reply.call_args.args[3], store.effective(10))
            await service.notices.render("已更新", 10)
            self.assertEqual(llm.rephrase_notice.call_args.args[1], store.effective(10))
            before = path.read_text(encoding="utf8")
            for command in ("/人设 未知", "/人设 设置 新人设", "/人设 ../persona"):
                await commands.run((99, 10, 1), command, None)
                self.assertEqual(path.read_text(encoding="utf8"), before)
            self.assertIn("私聊", await commands.run((99, -1, 1), "/人设 幽默群友", None))
            self.assertEqual(path.read_text(encoding="utf8"), before)
            with patch.object(settings, "write", side_effect=OSError):
                result = await commands.run((99, 10, 1), "/人设 幽默群友", None)
                self.assertIn("失败", result)
            self.assertEqual(path.read_text(encoding="utf8"), before)
            (personas / "新 人设.txt").write_text("本地新增", encoding="utf8")
            self.assertIn("新 人设", await commands.run((99, 10, 1), "/人设 列表", None))
            await commands.run((99, 10, 1), "/人设 切换 新 人设", None)
            self.assertEqual(store.effective(10)["persona"], "本地新增")
            (personas / "新 人设.txt").write_text("本地修改", encoding="utf8")
            self.assertEqual(store.effective(10)["persona"], "本地修改")
            (personas / "新 人设.txt").unlink()
            with self.assertRaises(ValueError):
                store.effective(10)
            await commands.run((99, 10, 1), "/人设 默认", None)
            self.assertEqual(store.effective(10), store.effective())
            self.assertNotIn("activity", settings.get(10))

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
