"""Style program notices without changing authoritative operation results."""

import asyncio
import json
import re
import time
from collections import OrderedDict


class Notices:
    def __init__(self, llm, profiles, limits):
        self.llm, self.profiles, self.limits = llm, profiles, limits
        self.cache = OrderedDict()

    async def render(self, source):
        # Structured help/fact rows remain verbatim; only their introduction is restyled.
        head, separator, tail = source.partition("\n")
        try:
            personality = self.profiles.effective()
            key = (source, json.dumps(personality, ensure_ascii=False, sort_keys=True))
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            if self.limits.semaphore.locked():
                return source
            async with asyncio.timeout(8):
                async with self.limits.slot():
                    rewritten = await self.llm.rephrase_notice(head, personality)
            if not isinstance(rewritten, str) or not rewritten.strip() or len(rewritten) > 1200:
                return source
            # Commands, identifiers, numeric limits and confirmation tokens cannot change.
            pattern = r"/[\w]+|[0-9a-f]{8,}|\d+|@[\w]+"
            if set(re.findall(pattern, head)) != set(re.findall(pattern, rewritten)):
                return source
            output = rewritten.strip() + (separator + tail if separator else "")
            self.cache[key] = (time.monotonic(), output)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return output
        except Exception:
            # Never suppress the actual result because the styling provider is unavailable.
            return source
