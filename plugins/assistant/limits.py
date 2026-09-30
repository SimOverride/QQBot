import asyncio
import time
from contextlib import asynccontextmanager

from .config import Config


class Limits:
    def __init__(self, config: Config):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.llm_max_concurrency)
        self.seen: dict[tuple, float] = {}
        self.cooldowns: dict[tuple, float] = {}
        self.pending = 0

    def duplicate(self, key: tuple) -> bool:
        now = time.monotonic()
        if self.seen.get(key, 0) > now:
            return True
        self.seen[key] = now + self.config.dedup_ttl_seconds
        return False

    def cooling(self, key: tuple) -> bool:
        now = time.monotonic()
        if self.cooldowns.get(key, 0) > now:
            return True
        self.cooldowns[key] = now + self.config.user_cooldown_seconds
        return False

    @asynccontextmanager
    async def slot(self):
        await asyncio.wait_for(self.semaphore.acquire(), self.config.queue_timeout_seconds)
        try:
            yield
        finally:
            self.semaphore.release()

    def cleanup(self):
        now = time.monotonic()
        for mapping in (self.seen, self.cooldowns):
            for key, expires in list(mapping.items()):
                if expires <= now:
                    del mapping[key]
