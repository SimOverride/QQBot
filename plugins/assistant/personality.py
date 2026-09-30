"""Local defaults and persistent group-specific persona and style selections."""

from pathlib import Path

DEFAULTS = {"persona": "你是一个友善、可靠的QQ群AI助手。", "style": "自然简洁，先说重点。"}
LIMITS = {"persona": 4000, "style": 4000}


class ProfileStore:
    def __init__(self, persona_path: Path | None = None, style_path: Path | None = None):
        self.paths = {"persona": persona_path, "style": style_path}
        self.directories = {
            field: path.parent if path else None for field, path in self.paths.items()
        }
        self.group_settings = None
        self.effective()  # Fail startup on missing, empty or malformed local configuration.

    def choices(self, field):
        directory = self.directories[field]
        if directory is None:
            return []
        return sorted(
            path.stem for path in directory.glob("*.txt")
            if path.is_file() and path.resolve().parent == directory.resolve()
            and path.stem != "默认"
        )

    def read_selection(self, field, name):
        if name not in self.choices(field):
            raise ValueError("未知或已移除的本地提示词，请先查看列表")
        path = self.directories[field] / (name + ".txt")
        try:
            value = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            raise ValueError("无法读取本地提示词，请检查文件和UTF-8编码") from None
        if not 1 <= len(value) <= LIMITS[field]:
            raise ValueError("本地提示词须为1至4000字")
        return value

    def effective(self, group_id: int | None = None) -> dict[str, str]:
        values = {}
        for field, path in self.paths.items():
            if path is None:
                values[field] = DEFAULTS[field]
                continue
            try:
                value = path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeError):
                raise ValueError("无法读取本地对话配置，请检查文件和UTF-8编码。") from None
            if not 1 <= len(value) <= LIMITS[field]:
                raise ValueError(
                    f"本地对话配置 {path.name} 长度为 {len(value)} 字符，"
                    f"允许范围为 1～{LIMITS[field]} 字符，请检查文件。"
                )
            values[field] = value
        if group_id is not None and group_id > 0 and self.group_settings is not None:
            settings = self.group_settings.read().get("groups", {}).get(str(group_id), {})
            for field in LIMITS:
                selection = settings.get(field)
                if selection:
                    values[field] = self.read_selection(field, selection["name"])
        return values
