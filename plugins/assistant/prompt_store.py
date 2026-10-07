"""读取可维护提示词和群认知；覆盖值在每次请求时读取。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "prompts/catalog.json"


def read_console(root=None):
    """缺少维护配置时使用空覆盖，损坏文件不静默回退。"""
    path = (root or ROOT) / "data/console.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def validate_console(value):
    """导入时核验可维护配置，防止非法覆盖破坏后续请求。"""
    if not isinstance(value, dict) or set(value) - {"prompts", "groups"}:
        raise ValueError("后台配置结构无效")
    if any(not isinstance(value.get(k, {}), dict) for k in ("prompts", "groups")):
        raise ValueError("后台配置字段须为对象")
    defaults = catalog()
    for key, text in value.get("prompts", {}).items():
        if key not in defaults or not isinstance(text, str) or not 1 <= len(text.strip()) <= 20000:
            raise ValueError("后台提示词配置不兼容")
        if any("{" + p + "}" not in text for p in defaults[key]["placeholders"]):
            raise ValueError("后台提示词缺少动态占位符")
    for key, data in value.get("groups", {}).items():
        ids = key.split(":")
        if len(ids) != 2 or any(not n.isdigit() or int(n) <= 0 for n in ids):
            raise ValueError("群认知账号或群号无效")
        if (
            not isinstance(data, dict)
            or set(data) != {"summary", "knowledge"}
            or any(not isinstance(text, str) or len(text) > 6000 for text in data.values())
        ):
            raise ValueError("群认知内容无效")


def prompt_text(key, values=None, root=None):
    """仅替换声明的动态占位符，不解释其他 JSON 花括号。"""
    item = catalog()[key]
    text = read_console(root).get("prompts", {}).get(key, item["text"])
    for name, value in (values or {}).items():
        text = text.replace("{" + name + "}", str(value))
    return text


def refresh_tools(functions):
    """工具结构由代码校验，说明文字从维护配置实时读取。"""
    overrides = read_console().get("prompts", {})
    replacements = {
        v["text"]: overrides.get(k, v["text"])
        for k, v in catalog().items()
        if k.startswith("tool.")
    }

    def visit(value):
        if isinstance(value, dict):
            return {k: visit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    return visit(functions)


def group_knowledge(bot, group, root=None):
    """仅向当前群注入该群认知，避免跨群传播。"""
    return read_console(root).get("groups", {}).get(f"{bot}:{group}", {})
