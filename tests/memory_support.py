"""通过正式工具建立测试认知，不保留旧提取执行器。"""

from plugins.assistant.history_tools import HistoryTools
from plugins.assistant.memory_tools import execute


def write_memory(archive, key, source_id, _text, updates):
    session = HistoryTools(archive, key)
    results = []
    for item in updates:
        args = {
            "action": "read",
            "subject": key[2],
            "field": item["field"],
            "value": "",
            "revision": "",
            "sources": [source_id],
        }
        current = execute(session, args)
        results.append(
            execute(
                session,
                {
                    **args,
                    "action": "write",
                    "value": item["value"],
                    "revision": current.get("revision", ""),
                },
            )
        )
    return results
