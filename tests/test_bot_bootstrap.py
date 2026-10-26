"""独立进程验证正式启动入口的插件加载与配置迁移顺序。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BotBootstrapTests(unittest.TestCase):
    def test_plugin_loading_and_setup_with_runtime_lock(self):
        project = Path(__file__).resolve().parents[1]
        script = r"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
import bot

root = Path.cwd()
(root / "data").mkdir()
(root / "data/console.json").write_text(
    json.dumps({"prompts": {"llm.poke.0": "bootstrap-test"}}), encoding="utf-8"
)
bot.ROOT = root
assert "plugins.assistant" not in sys.modules
with patch.object(bot, "configure_logging"), patch.object(bot.nonebot, "run") as run:
    with bot.project_lock(root, runtime=True):
        bot.main()
    run.assert_called_once()
assert bot.nonebot.get_plugin("assistant") is not None
config = json.loads((root / "data/console.json").read_text(encoding="utf-8"))
assert "prompts" not in config
assert config["modules"]["interaction"]["prompts"]["llm.poke.0"] == "bootstrap-test"
assert list((root / "backups").glob("console-before-modules-*.json"))
"""
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-c", script, str(project)],
                cwd=folder,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
