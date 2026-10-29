"""统一能力定义、后台覆盖和群认知；每次请求读取最新覆盖。"""

import copy
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES_PATH = ROOT / "prompts/modules.json"
MIGRATION_PATH = ROOT / "prompts/migration.json"


@lru_cache(maxsize=1)
def modules():
    return json.loads(MODULES_PATH.read_text(encoding="utf-8"))


def description_paths(schema, prefix=""):
    """参数说明以结构路径绑定，支持对象和数组中的嵌套字段。"""
    result = {}
    if prefix or "description" in schema:
        result[prefix] = schema.get("description", "")
    for key, child in schema.get("properties", {}).items():
        result.update(description_paths(child, prefix + "." + key if prefix else key))
    if isinstance(schema.get("items"), dict):
        result.update(description_paths(schema["items"], prefix + "[]"))
    return result


def module_defaults(key):
    item = modules()[key]
    return {
        "prompts": {k: v["text"] for k, v in item["prompts"].items()},
        "tools": {
            k: {"description": v["description"], "parameters": description_paths(v["parameters"])}
            for k, v in item["tools"].items()
        },
    }


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def validate_module(key, value, partial=False):
    def visit(data, default):
        if isinstance(default, dict):
            if (
                not isinstance(data, dict)
                or set(data) - set(default)
                or (not partial and set(data) != set(default))
            ):
                raise ValueError("能力模块字段不完整或包含未知字段")
            for name, child in data.items():
                visit(child, default[name])
        elif (
            not isinstance(data, str)
            or not (0 if default == "" else 1) <= len(data.strip()) <= 20000
        ):
            raise ValueError("能力说明须为 1 至 20000 字")

    visit(value, module_defaults(key))
    for name, text in value.get("prompts", {}).items():
        if any(
            "{" + token + "}" not in text
            for token in modules()[key]["prompts"][name]["placeholders"]
        ):
            raise ValueError("能力提示词缺少动态占位符")


def sparse_module(key, value):
    validate_module(key, value)

    def diff(data, default):
        if isinstance(data, dict):
            return {k: diff(v, default[k]) for k, v in data.items() if v != default[k]}
        return data

    return diff(value, module_defaults(key))


def normalize_console(value):
    """只在导入边界识别旧字段，新配置优先；旧条目不进入运行时目录。"""
    if not isinstance(value, dict) or set(value) - {"prompts", "modules", "groups"}:
        raise ValueError("后台配置结构无效")
    if any(not isinstance(value.get(k, {}), dict) for k in ("prompts", "modules", "groups")):
        raise ValueError("后台配置字段须为对象")
    migrated = {}
    mapping = json.loads(MIGRATION_PATH.read_text(encoding="utf-8")) if value.get("prompts") else {}
    for key, text in value.get("prompts", {}).items():
        if key in {"llm.extract_facts.0", "llm.knowledge_cleanup.0"} and isinstance(text, str):
            continue
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 20000:
            raise ValueError("旧提示词内容无效")
        if key not in mapping:
            raise ValueError("未知旧提示词：" + key)
        if text == mapping[key]["default"]:
            continue
        for path in mapping[key]["paths"]:
            default = module_defaults(path[0])
            for part in path[1:]:
                default = default[part]
            # 完整旧默认段落替换为当前契约，自定义前后缀保留。
            migrated_text = text.replace(mapping[key]["default"], default)
            migrated_text = (
                migrated_text.replace("read_history", "search_messages")
                .replace("resolve_user", "resolve_entity（kind=user）")
                .replace("resolve_group", "resolve_entity（kind=group）")
            )
            target = migrated
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = migrated_text
    result = {
        "modules": merge(migrated, value.get("modules", {})),
        "groups": copy.deepcopy(value.get("groups", {})),
    }
    for key, data in result["modules"].items():
        if key not in modules():
            raise ValueError("未知能力模块")
        validate_module(key, data, partial=True)
    for key, data in result["groups"].items():
        ids = key.split(":")
        if len(ids) != 2 or any(not n.isdigit() or int(n) <= 0 for n in ids):
            raise ValueError("群认知账号或群号无效")
        if (
            not isinstance(data, dict)
            or set(data) != {"summary", "knowledge"}
            or any(not isinstance(t, str) or len(t) > 6000 for t in data.values())
        ):
            raise ValueError("群认知内容无效")
    return result


def validate_console(value):
    normalize_console(value)


def read_console(root=None):
    path = Path(root or ROOT) / "data/console.json"
    return (
        normalize_console(json.loads(path.read_text(encoding="utf-8")))
        if path.exists()
        else {"modules": {}, "groups": {}}
    )


def migrate_console(root=None):
    """持有迁移锁，备份旧配置后原子发布新结构。"""
    import uuid

    from migration import project_lock

    root = Path(root or ROOT)
    path = root / "data/console.json"
    if not path.exists():
        return
    with project_lock(root, name=".migration.lock"):
        original = path.read_bytes()
        value = json.loads(original)
        if "prompts" not in value:
            validate_console(value)
            return
        result = normalize_console(value)
        backup = root / "backups" / ("console-before-modules-" + uuid.uuid4().hex + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        pending = path.with_suffix(".pending")
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(path)


def module_value(key, root=None):
    return merge(module_defaults(key), read_console(root)["modules"].get(key, {}))


def prompt_text(key, values=None, root=None):
    """仅替换声明的动态占位符，不解释其他 JSON 花括号。"""
    module = next(k for k, v in modules().items() if key in v["prompts"])
    text = module_value(module, root)["prompts"][key]
    for name, value in (values or {}).items():
        text = text.replace("{" + name + "}", str(value))
    return text


def tool_definition(name):
    return copy.deepcopy(next(v["tools"][name] for v in modules().values() if name in v["tools"]))


def refresh_tools(functions, root=None):
    """按工具名、参数路径覆盖说明，结构与执行契约不允许后台修改。"""
    overrides = read_console(root)["modules"]
    definitions = {
        name: data
        for key in modules()
        for name, data in merge(module_defaults(key), overrides.get(key, {}))["tools"].items()
    }

    def apply(schema, descriptions, prefix=""):
        if prefix in descriptions:
            schema["description"] = descriptions[prefix]
        for key, child in schema.get("properties", {}).items():
            apply(child, descriptions, prefix + "." + key if prefix else key)
        if isinstance(schema.get("items"), dict):
            apply(schema["items"], descriptions, prefix + "[]")

    result = copy.deepcopy(functions)
    for function in result:
        data = definitions.get(function["name"])
        if data:
            function["description"] = data["description"]
            apply(function["parameters"], data["parameters"])
    return result


def group_knowledge(bot, group, root=None):
    """群号用于保留来源；全部上下文均可被同一机器人的对话引用。"""
    from .vault import Vault

    notes = Vault(root or ROOT, bot).list(kind="context")
    return {
        field: "\n".join(n["value"] for n in notes if n.get("field") == f"{group}:{field}")
        for field in ("summary", "knowledge", "群背景")
        if any(n.get("field") == f"{group}:{field}" for n in notes)
    }
