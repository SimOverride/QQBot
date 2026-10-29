"""统一本地人设与全局选择，内容包含性格和表达方式。"""

from pathlib import Path

DEFAULTS = {
    "persona": (
        "自然参与当前聊天，有自己的兴趣与态度。合理求助通常愿意帮忙，过分要求可以拒绝；"
        "决定回答时讲清楚，不编造事实或执行结果。"
    )
}
LIMITS = {"persona": 12000}


class ProfileStore:
    def __init__(self, persona_path: Path | None = None):
        self.paths = {"persona": persona_path}
        self.directories = {
            field: path.parent if path else None for field, path in self.paths.items()
        }
        self.group_settings = None
        self.effective()  # 启动时校验缺失、空白和格式错误的本地人设。

    def choices(self, field):
        directory = self.directories[field]
        if directory is None:
            return []
        return sorted(
            path.stem
            for path in directory.glob("*.txt")
            if path.is_file()
            and path.resolve().parent == directory.resolve()
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
            raise ValueError("本地人设须为1至12000字")
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
        if self.group_settings is not None:
            default_persona = self.group_settings.read().get("default_persona", "默认")
            if default_persona != "默认":
                values["persona"] = self.read_selection("persona", default_persona)
        return values
