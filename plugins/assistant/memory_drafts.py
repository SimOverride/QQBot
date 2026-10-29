"""私有包中的 Obsidian 稿件成对迁移，冲突全文隔离保留。"""

import json
import re
import shutil

from .vault import Vault, digest


def merge(root, staged, plan, bot):
    ids = set()
    for name in plan:
        if name.startswith("data/memory-obsidian/") or "/工作台/" in name:
            parts = name.split("/")
            if parts[2] != str(bot):
                raise ValueError("Obsidian 稿件账号与迁移账号不一致")
            ids.add(plan[name].stem)
    for ident in ids:
        base_name = f"data/memory-obsidian/{bot}/{ident}.json"
        draft_name = f"data/memory-vault/{bot}/工作台/{ident}.md"
        if base_name not in plan or draft_name not in plan:
            raise ValueError("Obsidian 稿件及审阅基线必须同时迁移")
        base, draft = plan[base_name], plan[draft_name]
        if base.stat().st_size > 100000 or draft.stat().st_size > 100000:
            raise ValueError("Obsidian 稿件过大")
        original = json.loads(base.read_text(encoding="utf-8"))
        value = draft.read_text(encoding="utf-8").strip()
        if (
            not isinstance(original, dict)
            or original.get("id") != ident
            or not re.fullmatch(r"[a-f0-9]{64}", str(original.get("revision", "")))
            or not isinstance(original.get("value"), str)
            or not isinstance(original.get("title"), str)
            or len(original["value"]) > 16000
            or len(value) > 16000
        ):
            raise ValueError("Obsidian 审阅基线无效")
        target_base, target_draft = root / base_name, root / draft_name
        if target_base.exists() != target_draft.exists():
            raise ValueError("目标 Obsidian 稿件不完整，请先恢复稿件和基线")
        if not target_base.exists():
            continue
        if (
            target_base.read_bytes() != base.read_bytes()
            or target_draft.read_bytes() != draft.read_bytes()
        ):
            vault = Vault(staged, bot)
            conflict_id = digest(["obsidian-draft", ident, original, value])[:32]
            prior = Vault(root, bot).target(conflict_id)
            if prior.exists():
                vault.path.mkdir(parents=True, exist_ok=True)
                shutil.copy2(prior, vault.target(conflict_id))
            elif not vault.get(conflict_id):
                vault.put(
                    {
                        "id": conflict_id,
                        "kind": "thought",
                        "subject": 0,
                        "field": "Obsidian 待审稿冲突：" + original["title"][:70],
                        "state": "quarantine",
                        "fixed": True,
                        "value": value,
                        "draft_origin": {"id": ident, "base": original},
                    },
                    manual=True,
                )
            path = vault.target(conflict_id)
            plan[path.relative_to(staged).as_posix()] = path
        shutil.copy2(target_base, base)
        shutil.copy2(target_draft, draft)
