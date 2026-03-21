"""账号池请求并发控制。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class AdjustableConcurrencyLimiter:
    """支持运行时调整上限的异步并发限制器。"""

    def __init__(self, limit: int = 4) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._condition = asyncio.Condition()

    def snapshot(self) -> dict[str, int]:
        return {'limit': self._limit, 'active': self._active}

    async def update_limit(self, limit: int) -> dict[str, int]:
        async with self._condition:
            self._limit = max(1, int(limit))
            self._condition.notify_all()
            return self.snapshot()

    async def acquire(self) -> None:
        async with self._condition:
            while self._active >= self._limit:
                await self._condition.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[dict[str, int]]:
        await self.acquire()
        try:
            yield self.snapshot()
        finally:
            await self.release()
