"""原图留档、权限、过期链接及迁移的行为回归。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import migration
from plugins.assistant.config import Config
from plugins.assistant.image_archive import ImageArchive
from plugins.assistant.longterm import LongTermMemory
from plugins.assistant.vision import Vision


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_reopen_permission_and_expired_url(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = LongTermMemory(root / "data/memory.sqlite3", Config(vision_max_images=1))
            self.addCleanup(archive.close)
            bot = SimpleNamespace(self_id="123")
            event = SimpleNamespace(
                user_id=2,
                group_id=10,
                message_id=7,
                reply=None,
                original_message=[
                    SimpleNamespace(type="image", data={"url": "https://qpic.cn/test"})
                    for _ in range(2)
                ],
            )
            archive.collect((123, 10, 2), 7, "[图片]", ["image"], 1)
            images = ImageArchive(archive)
            with patch(
                "plugins.assistant.image_archive.download_image",
                AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"),
            ):
                await images.collect(bot, event, 10)
            row = archive.db.execute("SELECT * FROM messages").fetchone()
            refs = archive.message_context(row)["images"]
            self.assertEqual(len(refs), 2)
            self.assertNotIn("source_url", refs[0])
            self.assertEqual(len(list(images.folder.iterdir())), 1)
            ident = refs[0]["id"]
            from plugins.assistant.history_tools import HistoryTools
            from plugins.assistant.llm import LLM
            from plugins.assistant.search import Search

            llm = LLM(None, archive.config, Search(None, archive.config))
            llm.step = AsyncMock(
                side_effect=[
                    (
                        "",
                        [
                            {
                                "name": "view_image",
                                "call_id": "read-picture",
                                "arguments": json.dumps({"id": ident}),
                            }
                        ],
                        [],
                    ),
                    ("看到了", [], []),
                ]
            )
            await llm.reply(
                [], "查看历史图片", "test", history_tools=HistoryTools(archive, (123, 10, 2))
            )
            messages = llm.step.call_args.args[0]
            self.assertTrue(
                any(
                    part.get("type") == "image_url"
                    for msg in messages
                    if isinstance(msg.get("content"), list)
                    for part in msg["content"]
                )
            )

            self.assertEqual(images.read(ident, 123, {11})[0]["status"], "not_found_or_forbidden")
            self.assertEqual(images.read(ident, 456, {10})[0]["status"], "not_found_or_forbidden")
            self.assertEqual(images.read("../../secret", 123, {10})[0]["status"], "invalid_id")
            for seg in event.original_message:
                seg.data.clear()
            parts = await Vision(archive.config, archive).prepare(bot, event)
            self.assertEqual(sum(p["type"] == "image_url" for p in parts), 1)
            self.assertTrue(any(ident in p.get("text", "") for p in parts))
            self.assertEqual(images.read(ident, 123, {10})[0]["status"], "ok")
            # 私有迁移包恢复原图与来源引用，目标账号明确指定。
            (root / ".env").write_text("ONEBOT_ACCESS_TOKEN=test\n", encoding="utf-8")
            bundle = root / "package.zip"
            migration.export_data(root, bundle)
            target = root / "target"
            target.mkdir()
            migration.import_data(target, bundle, 123)
            restored = LongTermMemory(target / "data/memory.sqlite3", Config())
            try:
                self.assertEqual(ImageArchive(restored).read(ident, 123, {10})[0]["status"], "ok")
            finally:
                restored.close()
            (images.folder / ident[6:]).unlink()
            self.assertEqual(images.read(ident, 123, {10})[0]["status"], "unavailable")
            archive.close()

    async def test_failed_image_is_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = LongTermMemory(Path(folder) / "data/memory.sqlite3", Config())
            try:
                archive.collect((1, -2, 2), 3, "[图片]", ["image"], 1)
                event = SimpleNamespace(
                    user_id=2,
                    message_id=3,
                    original_message=[SimpleNamespace(type="image", data={})],
                )
                await ImageArchive(archive).collect(SimpleNamespace(self_id="1"), event, -2)
                row = archive.db.execute("SELECT * FROM messages").fetchone()
                self.assertEqual(archive.message_context(row)["images"][0]["status"], "unavailable")
            finally:
                archive.close()

    async def test_context_delta_native_runtime(self):
        import os
        import subprocess

        from plugins.tasks.desktop import installation, launch

        try:
            package = installation()
        except ValueError:
            self.skipTest("未安装适配客户端")
        env = os.environ.copy()
        executable = launch(package, env)[0]
        result = subprocess.run(
            [executable, str(Path(__file__).with_name("dsh_context_check.mjs"))],
            env=env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
