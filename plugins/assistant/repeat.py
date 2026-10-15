"""跟踪纯文字复读链，模型只决定是否参与，不能改写原文。"""

import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class RepeatState:
    text: str = ""
    started: float = 0
    message_id: int = 0
    users: set = field(default_factory=set)
    attempts: dict = field(default_factory=dict)

    def observe(self, event, explicit, now=None):
        now = time.monotonic() if now is None else now
        segments = getattr(event, "original_message", ())
        valid = (
            not explicit
            and segments
            and all(s.type == "text" for s in segments)
            and getattr(event, "reply", None) is None
        )
        text = "".join(str(s.data.get("text", "")) for s in segments) if valid else ""
        if not text.strip() or text.lstrip().startswith("/") or len(text) > 4000:
            text = ""
        if text != self.text or now - self.started > 120:
            self.text, self.started, self.users = text, now, set()
        self.message_id = event.message_id
        if text:
            self.users.add(event.user_id)

    def candidate(self, message_id, now=None):
        now = time.monotonic() if now is None else now
        self.attempts = {k: v for k, v in self.attempts.items() if now - v < 1800}
        if (
            self.text
            and message_id == self.message_id
            and len(self.users) >= 2
            and now - self.started <= 120
            and self.digest(self.text) not in self.attempts
        ):
            return self.text
        return None

    def reserve(self, text):
        # 发送结果不确定时也不重试，避免平台已发送但响应丢失造成循环。
        self.attempts[self.digest(text)] = time.monotonic()

    @staticmethod
    def digest(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recently_sent(archive, bot, group, text):
    return (
        archive.db.execute(
            "SELECT 1 FROM messages WHERE bot=? AND grp=? AND usr=? AND content=? "
            "AND created>? LIMIT 1",
            (bot, group, bot, text, time.time() - 1800),
        ).fetchone()
        is not None
    )
