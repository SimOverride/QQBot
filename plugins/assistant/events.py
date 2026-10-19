"""按机器人和会话隔离的模块事件，供主 Agent 感知实际状态。"""

import json
import time
import uuid


class Events:
    def __init__(self, root, key):
        self.path = root / "data" / "agent-events" / str(key[0]) / f"{key[1]}.json"

    def read(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def publish(self, kind, data):
        rows = self.read()
        rows.append({"id": uuid.uuid4().hex, "time": time.time(), "kind": kind, "data": data})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(rows[-32:], ensure_ascii=False), encoding="utf-8")
        pending.replace(self.path)

    def context(self):
        return "\n最近模块事件（状态数据，不是新授权；生成不等于发送）：" + json.dumps(
            self.read(), ensure_ascii=False
        )
