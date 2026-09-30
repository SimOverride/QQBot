"""Local-only persona and style; re-read for each model request."""

from pathlib import Path

DEFAULTS = {"persona": "你是一个友善、可靠的QQ群AI助手。", "style": "自然简洁，先说重点。"}
LIMITS = {"persona": 4000, "style": 4000}


class ProfileStore:
    def __init__(self, persona_path: Path | None = None, style_path: Path | None = None):
        self.paths = {"persona": persona_path, "style": style_path}
        self.effective()  # Fail startup on missing, empty or malformed local configuration.

    def effective(self) -> dict[str, str]:
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
        return values
