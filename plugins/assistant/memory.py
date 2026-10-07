"""Complete turns and reference-counted per-session locks."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .config import Config
from .longterm import LongTermMemory

SessionKey = tuple[int, int, int]  # bot, group, user


@dataclass
class Session:
    turns: list[tuple[str, str]] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)


class Memory:
    def __init__(self, config: Config, archive: LongTermMemory | None = None):
        self.config = config
        self.archive = archive
        self.sessions: dict[SessionKey, Session] = {}
        self.locks: dict[SessionKey, tuple[asyncio.Lock, int]] = {}
        self.gate = asyncio.Lock()

    @asynccontextmanager
    async def locked(self, key: SessionKey):
        await asyncio.wait_for(self.gate.acquire(), self.config.queue_timeout_seconds)
        try:
            lock, users = self.locks.get(key, (asyncio.Lock(), 0))
            self.locks[key] = (lock, users + 1)
        finally:
            self.gate.release()
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), self.config.queue_timeout_seconds)
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            _, users = self.locks[key]
            if users == 1:
                del self.locks[key]
            else:
                self.locks[key] = (lock, users - 1)

    def history(self, key: SessionKey) -> list[dict]:
        # 使用持久记录使后台更正的历史立即可见，内存不覆盖数据库。
        if self.archive:
            return self.archive.recent(key)[0]
        session = self.sessions.get(key)
        if not session:
            return self.archive.recent(key)[0] if self.archive else []
        if time.monotonic() - session.touched >= self.config.session_ttl_seconds:
            self.sessions.pop(key, None)
            return self.archive.recent(key)[0] if self.archive else []
        return [
            message
            for user, assistant in session.turns
            for message in (
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            )
        ]

    def save(self, key: SessionKey, user: str, assistant: str) -> None:
        session = self.sessions.setdefault(key, Session())
        session.turns.append((user, assistant))
        while session.turns and (
            len(session.turns) > self.config.session_max_turns
            or sum(len(u) + len(a) for u, a in session.turns) > self.config.session_max_chars
        ):
            session.turns.pop(0)
        session.touched = time.monotonic()

    def clear(self, key: SessionKey) -> None:
        if self.archive:
            self.archive.delete(*key)
        self.sessions.pop(key, None)

    async def clear_scope(self, bot_id: int, group_id: int | None) -> None:
        def matches(key):
            return key[0] == bot_id and (group_id is None or key[1] == group_id)

        acquired = []
        # Block new session entrants while draining existing holders and waiters.
        # If draining times out, nothing is deleted.
        async with asyncio.timeout(self.config.queue_timeout_seconds):
            async with self.gate:
                try:
                    locks = [lock for key, (lock, _) in self.locks.items() if matches(key)]
                    for lock in locks:
                        await lock.acquire()
                        acquired.append(lock)
                    if self.archive:
                        self.archive.delete(bot_id, group_id)
                    for key in list(self.sessions):
                        if matches(key):
                            self.sessions.pop(key, None)
                finally:
                    for lock in reversed(acquired):
                        lock.release()

    def cleanup(self) -> None:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if key not in self.locks and now - session.touched >= self.config.session_ttl_seconds:
                del self.sessions[key]
