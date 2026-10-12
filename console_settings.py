"""本机设置与插件管理：白名单配置、密钥只写、版本冲突检查。"""

import hashlib
import shutil
import uuid
from pathlib import Path

from dotenv import dotenv_values, set_key
from pydantic import ValidationError

import migration
from console_store import atomic_json, revision
from plugins.assistant.config import Config
from plugins.assistant.groupchat import GroupSettings
from plugins.extensions.store import Store

# 仅开放运行参数；身份、监听地址、启动路径不由此页面修改。
FIELDS = {
    "模型": {
        "llm_provider": "模型服务",
        "deepseek_model": "DeepSeek 型号",
        "openai_model": "OpenAI 型号",
        "llm_thinking_enabled": "开启深度思考",
        "llm_max_output_tokens": "最大输出 Token",
    },
    "上下文": {
        "session_max_turns": "上下文轮数",
        "session_max_chars": "上下文字符上限",
        "session_ttl_seconds": "上下文有效时间（秒）",
        "input_max_chars": "输入字符上限",
        "history_max_calls": "每次回答历史查询次数",
        "memory_profile_min_messages": "认知提取最少消息数",
        "knowledge_cleanup_enabled": "自动整理认知",
        "knowledge_cleanup_interval_hours": "认知整理间隔（小时）",
    },
    "请求限制": {
        "llm_timeout_seconds": "模型请求超时（秒）",
        "request_timeout_seconds": "完整回答超时（秒）",
        "llm_max_concurrency": "模型并发数",
        "max_pending_requests": "等待请求上限",
        "queue_timeout_seconds": "队列等待时间（秒）",
        "user_cooldown_seconds": "用户冷却（秒）",
        "reply_chunk_chars": "每条回复字符数",
        "reply_max_messages": "回复消息条数上限",
    },
    "搜索和图片": {
        "search_max_results": "搜索结果数量",
        "search_max_calls": "每次回答搜索次数",
        "search_timeout_seconds": "搜索超时（秒）",
        "vision_max_images": "每次识别图片数",
        "emotes_auto_collect": "自动收集表情",
        "emotes_daily_import_limit": "每日收集表情上限",
        "emotes_max_bytes": "表情文件最大字节数",
    },
}
KEYS = {key for fields in FIELDS.values() for key in fields}
SECRETS = {
    "deepseek_api_key": "DeepSeek 密钥",
    "openai_api_key": "OpenAI 密钥",
    "search_api_key": "搜索密钥",
}


class Settings:
    def __init__(self, root):
        self.root = Path(root)

    def check(self):
        if (self.root / migration.JOURNAL).exists():
            raise ValueError("导入尚未恢复，暂不能修改设置")

    def env_revision(self):
        path = self.root / ".env"
        return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()

    def env(self):
        raw = dotenv_values(self.root / ".env")
        try:
            cfg = Config.model_validate(
                {
                    k.lower(): v
                    for k, v in raw.items()
                    if k.lower() in KEYS | set(SECRETS) and v is not None
                }
            )
        except ValidationError:
            raise ValueError("现有运行配置无效，请检查 .env 参数") from None
        schema = Config.model_json_schema()["properties"]
        return {
            "revision": self.env_revision(),
            "groups": [
                {
                    "name": name,
                    "fields": [
                        {
                            "key": key,
                            "label": label,
                            "value": getattr(cfg, key),
                            "type": schema[key].get("type", "string"),
                            "options": schema[key].get("enum"),
                            "min": schema[key].get(
                                "minimum", schema[key].get("exclusiveMinimum", 0)
                            ),
                            "max": schema[key].get("maximum"),
                        }
                        for key, label in fields.items()
                    ],
                }
                for name, fields in FIELDS.items()
            ],
            "secrets": [
                {"key": key, "label": label, "configured": bool(raw.get(key.upper()))}
                for key, label in SECRETS.items()
            ],
        }

    def save_env(self, values, secrets, expected):
        if not isinstance(values, dict) or set(values) != KEYS or not isinstance(secrets, dict):
            raise ValueError("配置字段不完整")
        if set(secrets) - set(SECRETS) or any(
            not isinstance(v, str) or not v.strip() or len(v) > 4096 or "\n" in v or "\r" in v
            for v in secrets.values()
        ):
            raise ValueError("密钥替换内容无效，留空请不要提交")
        try:
            Config.model_validate(values | secrets)
        except ValidationError as error:
            fields = sorted({str(e["loc"][0]) for e in error.errors(include_input=False)})
            raise ValueError("配置值无效：" + "、".join(fields)) from None
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check()
            if expected != self.env_revision():
                raise ValueError("配置已变化，请刷新设置后重试")
            path = self.root / ".env"
            previous = path.read_bytes() if path.exists() else b""
            backup = self.root / "backups/settings" / (uuid.uuid4().hex + ".env")
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(previous)
            temporary = self.root / (".env-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(previous)
                for key, value in (values | secrets).items():
                    encoded = str(value).lower() if isinstance(value, bool) else str(value)
                    set_key(temporary, key.upper(), encoded, quote_mode="always")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return {"message": "已保存；重启机器人后生效，后台后续模型请求使用新配置", **self.env()}

    def defaults(self):
        data = GroupSettings(self.root / "group_chat.json").read()
        return {
            "revision": revision(data),
            "activity": data.get("default_activity", 50),
            "persona": data.get("default_persona", "默认"),
            "personas": sorted(p.stem for p in (self.root / "personas").glob("*.txt")),
        }

    def save_defaults(self, activity, persona, expected):
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check()
            current = self.defaults()
            if current["revision"] != expected:
                raise ValueError("默认设置已变化，请刷新后重试")
            if (
                type(activity) is not int
                or not 0 <= activity <= 100
                or persona not in current["personas"]
            ):
                raise ValueError("默认积极性或人设无效")
            store = GroupSettings(self.root / "group_chat.json")
            data = store.read()
            atomic_json(self.root / "backups/settings" / (uuid.uuid4().hex + ".json"), data)
            data.update(default_activity=activity, default_persona=persona)
            store.write(data)
        return {
            "message": "默认设置已保存，未单独配置的群及默认人设后续请求生效",
            **self.defaults(),
        }

    def plugins(self, bot=None, group=None):
        value = Store(self.root).read()
        rows = []
        for account, entries in value["bots"].items():
            if bot is not None and int(account) != bot:
                continue
            for name, entry in entries.items():
                if (
                    group is not None
                    and entry["scope"] is not None
                    and str(group) not in entry["scope"]
                ):
                    continue
                metadata = next(
                    (d["_runtime"] for d in entry["data"].values() if "_runtime" in d), {}
                )
                rows.append(
                    {
                        "bot": account,
                        "id": name,
                        "title": metadata.get("title", "定时消息" if name == "schedule" else name),
                        "enabled": entry["enabled"],
                        "scope": entry["scope"],
                        "group_enabled": entry["groups"].get(str(group), False),
                        "deletable": name == "schedule" or name.startswith("gen_"),
                    }
                )
        return {"revision": revision(value), "items": rows}

    def switch(self, bot, name, enabled, expected, group=None):
        if type(enabled) is not bool or (
            group is not None and (type(group) is not int or group <= 0)
        ):
            raise ValueError("插件开关参数无效")
        with migration.project_lock(self.root, name=".migration.lock"):
            self.check()
            store = Store(self.root)
            value = store.read()
            if revision(value) != expected:
                raise ValueError("插件状态已变化，请刷新列表后重试")
            entry = value["bots"].get(str(bot), {}).get(name)
            if entry is None:
                raise ValueError("插件不存在")
            if group is None:
                entry["enabled"] = enabled
            else:
                if entry["scope"] is not None and str(group) not in entry["scope"]:
                    raise ValueError("此插件不适用于该群")
                entry["groups"][str(group)] = enabled
            atomic_json(self.root / "backups/plugins" / (uuid.uuid4().hex + ".json"), store.read())
            store.write(value)
        return {"message": "插件开关已保存"}

    def delete_plugins(self, selected, expected):
        if not isinstance(selected, list) or not selected or len(selected) > 100:
            raise ValueError("请选择 1 至 100 个插件")
        # 卸载需与机器人执行及离线导入互斥，避免仍在运行的插件发布或写回。
        with (
            migration.project_lock(self.root),
            migration.project_lock(self.root, name=".migration.lock"),
        ):
            self.check()
            store = Store(self.root)
            value = store.read()
            if revision(value) != expected:
                raise ValueError("插件列表已变化，请刷新后重试")
            folders = set()
            seen = set()
            for item in selected:
                if not isinstance(item, dict) or set(item) != {"bot", "id"}:
                    raise ValueError("插件选择无效")
                bot, name = str(item["bot"]), item["id"]
                if not isinstance(name, str) or (bot, name) in seen:
                    raise ValueError("插件选择重复或无效")
                seen.add((bot, name))
                entry = value["bots"].get(bot, {}).get(name)
                if entry is None or not (name == "schedule" or name.startswith("gen_")):
                    raise ValueError("插件不存在或属于受保护核心模块")
                del value["bots"][bot][name]
                if name.startswith("gen_"):
                    folders.add(name)
            backup = self.root / "backups/plugins" / uuid.uuid4().hex
            backup.mkdir(parents=True)
            shutil.copyfile(store.path, backup / "plugins.json")
            moved = []
            try:
                for name in folders:
                    if any(name in entries for entries in value["bots"].values()):
                        continue
                    folder = migration.safe_path(self.root, "data/generated/" + name)
                    if folder.exists():
                        destination = backup / name
                        folder.rename(destination)
                        moved.append((folder, destination))
                store.write(value)
            except Exception:
                for folder, destination in reversed(moved):
                    destination.rename(folder)
                raise
        return {"message": "已卸载所选插件及运行数据；原文件保留在本机备份中"}
