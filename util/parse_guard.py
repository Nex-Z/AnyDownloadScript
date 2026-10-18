"""Per-process request coalescing, concurrency limits and challenge cooldown."""
import asyncio
import math
import time
from collections import OrderedDict

from util.upstream import ParseError


class ParseGuard:
    def __init__(self):
        self.pending = {}
        self.cooldowns = OrderedDict()
        self.semaphore = asyncio.Semaphore(2)

    async def run(self, parser, url):
        key = (type(parser).__name__, url)
        now = time.monotonic()
        if key in self.cooldowns:
            until, error = self.cooldowns[key]
            if until > now:
                raise ParseError(error.status_code, error.detail, error.error_code,
                                 error.stage, error.retryable, math.ceil(until - now))
            del self.cooldowns[key]
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 32:
                raise ParseError(503, "解析任务较多，请稍后重试", "PARSER_BUSY", "queue", True, 5)
            task = asyncio.create_task(self._execute(parser, url, key))
            self.pending[key] = task
            # Consume exceptions even if the only client disconnected.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)

    async def _execute(self, parser, url, key):
        try:
            async with asyncio.timeout(60):
                async with self.semaphore:
                    return await parser.parse(url)
        except TimeoutError as exc:
            raise ParseError(504, "解析超过 60 秒，请稍后重试", "PARSE_TIMEOUT", "parse", True) from exc
        except ParseError as exc:
            if exc.error_code in ("VERIFICATION_REQUIRED", "UPSTREAM_RATE_LIMIT"):
                self.cooldowns[key] = (time.monotonic() + 60, exc)
                while len(self.cooldowns) > 256:
                    self.cooldowns.popitem(last=False)
            raise
        finally:
            self.pending.pop(key, None)


parse_guard = ParseGuard()
