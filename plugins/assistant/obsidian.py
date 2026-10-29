"""同库 Obsidian 导航与待审编辑稿；正式写入仍走后台差异及版本检查。"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

import migration

from .vault import Vault, atomic


def installed():
    """只检查客户端登记，不启动程序、不下载软件。"""
    if os.name != "nt":
        import shutil

        return bool(shutil.which("obsidian"))
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"obsidian\shell\open\command"):
            return True
    except OSError:
        return False


def vault_registration(folder):
    """只读客户端仓库登记，不能把协议可用误当成仓库已接入。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        config = Path(base) / "obsidian/obsidian.json" if base else None
    elif sys.platform == "darwin":
        config = Path.home() / "Library/Application Support/obsidian/obsidian.json"
    else:
        config = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "obsidian/obsidian.json"
        )
    if config is None or not config.exists():
        return {"state": "unregistered", "id": None}
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        vaults = data.get("vaults")
        if not isinstance(vaults, dict):
            raise ValueError("仓库登记格式无效")
        target = os.path.normcase(str(folder.resolve()))
        for ident, item in vaults.items():
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            if os.path.normcase(str(Path(item["path"]).resolve())) == target:
                return {"state": "registered", "id": str(ident)}
        return {"state": "unregistered", "id": None}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"state": "unknown", "id": None}


class Workbench:
    def __init__(self, root, bot):
        self.root = Path(root).resolve()
        self.vault = Vault(root, bot)
        self.bot = bot
        self.state = self.root / "data/memory-obsidian" / str(bot)

    def safe(self, path):
        """拒绝工作台、导航和状态目录中的符号链接及越界路径。"""
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Obsidian 路径超出项目")
        for item in (path, *path.parents):
            if item == self.root:
                break
            if item.is_symlink():
                raise ValueError("Obsidian 路径不能使用符号链接")
        return path

    def uri(self, file="导航/记忆库.md"):
        registration = vault_registration(self.vault.path)
        if registration["state"] != "registered":
            return "obsidian://choose-vault"
        return "obsidian://open?" + urlencode({"vault": registration["id"], "file": file})

    def launch(self, note=None):
        """仅打开本项目生成的链接，由本机系统调用客户端并报告启动错误。"""
        result = self.draft(note) if note is not None else self.prepare()
        uri = result["uri"]
        try:
            if sys.platform == "win32":
                os.startfile(uri)
            else:
                subprocess.run(
                    ["open" if sys.platform == "darwin" else "xdg-open", uri],
                    check=True,
                    timeout=10,
                    capture_output=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("无法启动 Obsidian，请确认客户端已安装并手动打开一次后重试。") from exc
        result["launch_message"] = (
            (
            "已向系统发送打开请求，请查看 Obsidian 窗口；"
            "首次使用需选择打开文件夹作为仓库。"
        )
        )
        return result

    def status(self):
        pending = []
        for path in sorted(self.state.glob("*.json")):
            if re.fullmatch(r"[a-f0-9]{32}", path.stem):
                review = self.review(path.stem)
                if review["changed"]:
                    pending.append({k: review[k] for k in ("id", "title", "conflict")})
        return {
            "installed": installed(),
            "registration": vault_registration(self.vault.path)["state"],
            "setup_hint": (
                "在 Obsidian 仓库管理器选择‘打开文件夹作为仓库’，"
                "选择下方完整目录；完成后返回刷新状态。"
            ),
            "vault": str(self.vault.path),
            "uri": self.uri(),
            "prepared": (self.vault.path / "导航/记忆库.md").exists(),
            "drafts": pending,
        }

    def exclude_views(self):
        ignore = self.safe(self.vault.path / ".gitignore")
        ignored = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        for pattern in ("工作台/", "导航/", "图谱/", ".obsidian/"):
            if pattern not in ignored.splitlines():
                ignored = ignored.rstrip() + "\n" + pattern + "\n"
        atomic(ignore, ignored)

    def prepare(self):
        """刷新可重建导航，绝不覆盖编辑稿或正式记忆。"""
        with migration.project_lock(self.root, name=".migration.lock"):
            if (self.root / migration.JOURNAL).exists():
                raise ValueError("请先恢复未完成的数据迁移")
            folder = self.safe(self.vault.path / "导航")
            overview = self.vault.overview()
            self.exclude_views()
            from .knowledge import read_directory

            names = read_directory(self.root).get(str(self.bot), {}).get("users", {})
            for entry in overview:
                if entry["category"] in ("人物", "事件"):
                    name = names.get(str(entry["subject"]), {}).get("name")
                    if name:
                        entry["title"] = str(name) + (
                            "的档案" if entry["category"] == "人物" else "的事件"
                        )
            pages = {}
            for entry in overview:
                title = re.sub(r'[<>:"/\\|?*\[\]\x00-\x1f]', " ", entry["title"]).strip()[:60]
                suffix = hashlib.sha256(entry["key"].encode()).hexdigest()[:8]
                pages[entry["key"]] = (title or entry["category"]) + "-" + suffix
            manifest = self.safe(self.state / "graph.json")
            previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
            current_pages = {value + ".md" for value in pages.values()}
            for filename, fingerprint in previous.items():
                if filename in current_pages or Path(filename).name != filename:
                    continue
                stale = self.safe(self.vault.path / "图谱" / filename)
                if stale.exists() and hashlib.sha256(stale.read_bytes()).hexdigest() == fingerprint:
                    stale.unlink()
            generated = {}
            for entry in overview:
                own = {n["id"] for n in entry["notes"]}
                related = [
                    other
                    for other in overview
                    if other["key"] != entry["key"]
                    and (
                        own & {n["id"] for n in other["notes"]}
                        or (entry["subject"] and entry["subject"] == other["subject"])
                    )
                ]
                content = (
                    "# "
                    + entry["title"]
                    + "\n\n> 自动生成的阅读视图，修改请使用后台工作台编辑稿。\n\n"
                    + entry["content"]
                )
                content += "\n\n## 依据\n" + "\n".join(
                    f"- [[{n['id']}|{n.get('field', n['kind'])}]]" for n in entry["notes"]
                )
                if related:
                    content += "\n\n## 相同人物或共同依据\n" + "\n".join(
                        f"- [[图谱/{pages[n['key']]}|{n['title']}]]" for n in related
                    )
                atomic(
                    self.safe(self.vault.path / "图谱" / (pages[entry["key"]] + ".md")),
                    content + "\n",
                )
                filename = pages[entry["key"]] + ".md"
                generated[filename] = hashlib.sha256(
                    (self.vault.path / "图谱" / filename).read_bytes()
                ).hexdigest()
            atomic(manifest, json.dumps(generated, ensure_ascii=False))
            lines = [
                "# 记忆库",
                "",
                "这里浏览机器人使用的同一份记忆。正式笔记用于阅读；修改请在后台创建工作台编辑稿，",
                "编辑后回后台查看差异并确认保存。工作台稿件不进入机器人召回。",
                "",
            ]
            for category in dict.fromkeys(g["category"] for g in overview):
                lines += [f"## {category}", ""]
                for entry in overview:
                    if entry["category"] != category:
                        continue
                    title = str(entry["title"]).replace("\n", " ")
                    lines += [
                        f"### [[图谱/{pages[entry['key']]}|{title}]]",
                        "",
                        entry["content"],
                        "",
                    ]
                    for note in entry["notes"]:
                        name = note.get("field", note["kind"]).replace("|", " ").replace("]", "")
                        lines.append(f"- [[{note['id']}|{name}]]")
                    lines.append("")
            atomic(folder / "记忆库.md", "\n".join(lines))
            config = self.safe(self.vault.path / ".obsidian/graph.json")
            if not config.exists():
                atomic(config, json.dumps({"search": "path:图谱", "showOrphans": True}))
        return self.status()

    def draft(self, ident):
        self.vault.target(ident)
        with migration.project_lock(self.root, name=".migration.lock"):
            if (self.root / migration.JOURNAL).exists():
                raise ValueError("请先恢复未完成的数据迁移")
            self.exclude_views()
            note = self.vault.get(ident)
            if note is None:
                raise ValueError("记忆不存在")
            base = self.safe(self.state / (ident + ".json"))
            draft = self.safe(self.vault.path / "工作台" / (ident + ".md"))
            if base.exists() or draft.exists():
                if not base.exists() or not draft.exists():
                    raise ValueError("工作台稿件与基线不完整，请恢复原稿件和基线")
                return {**self.review(ident), "uri": self.uri("工作台/" + ident + ".md")}
            # 基线保存在库外，Obsidian 正文编辑不会修改版本凭据。
            atomic(
                base,
                json.dumps(
                    {
                        "id": ident,
                        "revision": note["revision"],
                        "value": note["value"],
                        "title": note.get("field", note["kind"]),
                    },
                    ensure_ascii=False,
                ),
            )
            atomic(draft, note["value"] + "\n")
        return {**self.review(ident), "uri": self.uri("工作台/" + ident + ".md")}

    def review(self, ident):
        self.vault.target(ident)
        base = self.safe(self.state / (ident + ".json"))
        draft = self.safe(self.vault.path / "工作台" / (ident + ".md"))
        if not base.exists() or not draft.exists():
            raise ValueError("工作台编辑稿不存在")
        if draft.stat().st_size > 100000 or base.stat().st_size > 100000:
            raise ValueError("工作台文件过大")
        original = json.loads(base.read_text(encoding="utf-8"))
        value = draft.read_text(encoding="utf-8").strip()
        if len(value) > 16000:
            raise ValueError("记忆正文最多16000字")
        current = self.vault.get(ident)
        receipt = hashlib.sha256(
            json.dumps([original, value], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return {
            "id": ident,
            "title": original["title"],
            "before": original["value"],
            "after": value,
            "revision": receipt,
            "changed": value != original["value"],
            "conflict": not current or current["revision"] != original["revision"],
        }

    def apply(self, store, ident, expected):
        review = self.review(ident)
        if review["revision"] != expected or review["conflict"]:
            raise ValueError("记忆或稿件已变化，请重新核对；未覆盖现有内容")
        resource = f"memory:{self.bot}:{ident}"
        current = store.get(resource)
        base = json.loads((self.state / (ident + ".json")).read_text(encoding="utf-8"))
        if current["note_revision"] != base["revision"]:
            raise ValueError("记忆版本冲突，请重新核对")
        saved = store.put(
            resource,
            {**current["value"], "content": review["after"], "fixed": True},
            current["revision"],
        )
        # 保存后重置对应稿件基线；若并发人工编辑已变化，保留为下一次待审修改。
        atomic(
            self.state / (ident + ".json"),
            json.dumps(
                {
                    "id": ident,
                    "revision": saved["note_revision"],
                    "value": review["after"],
                    "title": review["title"],
                },
                ensure_ascii=False,
            ),
        )
        return saved
