"""自然分条只处理普通聊天文本，保留结构化内容与显式边界。"""

import re


def split_chat(text, maximum=3):
    """按换行和完整句子分条，短回应合并，多余意思保留在末条。"""
    if not text.strip():
        return []
    if maximum <= 1 or re.search(
        r"```|~~~|`|https?://|(?m:^\s*(?:[/#>|]|[-*+]\s|\d+[.)、]\s))", text
    ):
        return [text]
    parts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pieces = re.split(r"(?<=[。！？!?])\s*(?=[^”’」』])", line) if len(line) > 40 else [line]
        for piece in filter(None, pieces):
            if parts and len(parts[-1]) < 6 and len(parts[-1]) + len(piece) <= 24:
                parts[-1] += piece
            else:
                parts.append(piece)
    if len(parts) > maximum:
        parts = parts[: maximum - 1] + ["\n".join(parts[maximum - 1 :])]
    return parts or [text]
