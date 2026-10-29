"""人设检查与固定场景试聊，只使用当前人设和虚构输入。"""

import re

SCENARIOS = {
    "chat": {"title": "普通闲聊", "text": "今天下班路上看到一只猫，盯着我看了半天。"},
    "help": {"title": "认真求助", "text": "明天要做五分钟的自我介绍，我有点紧张，怎么准备比较好？"},
    "joke": {"title": "接梗", "text": "我决定早睡，所以今晚先熬夜研究一下怎么早睡。"},
    "correct": {"title": "接受纠正", "text": "你刚才理解反了，我是周五出发，不是周五回来。"},
    "end": {"title": "结束话题", "text": "好，我知道了，先去忙了，回头聊。"},
}


def inspect_persona(value):
    """仅提示可疑之处，不把关键词命中当作质量评分或保存限制。"""
    warnings = []
    checks = [
        (r"说话|语气|口吻|措辞|表达", "未找到明确的说话方式，可补充语气、措辞和篇幅偏好。"),
        (r"例如|示例|比如|例子", "未找到回答示例，可补充闲聊、求助和收尾的典型表达。"),
        (
            r"不要|不能|不得|避免|禁止|边界|不做",
            "未找到明确边界，可说明哪些场合应收住玩笑或停止追问。",
        ),
    ]
    for pattern, warning in checks:
        if not re.search(pattern, value):
            warnings.append(warning)
    if re.search(r"(?:始终|总是|每次|必须).{0,8}(?:简短|短句|一句)", value) and re.search(
        r"(?:始终|总是|每次|必须).{0,8}(?:详细|长篇|全面)", value
    ):
        warnings.append("可能同时要求始终简短和每次详细，请检查适用场景是否区分。")
    if any(len(line) > 400 for line in value.splitlines()):
        warnings.append("存在超过400字的段落，可按主题拆开以便维护。")
    return {
        "warnings": warnings,
        "note": "规则检查仅供参考；未发现提示不代表没有矛盾，也不阻止保存。",
    }
