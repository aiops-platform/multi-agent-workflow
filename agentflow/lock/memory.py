# -*- coding: utf-8 -*-
"""InMemory Lock（本地测试 / 单进程 MVP）。"""
from __future__ import annotations

import asyncio
from typing import Optional

from .base import Lock


class InMemoryLock(Lock):
    def __init__(self) -> None:
        self._holders: dict[str, float] = {}  # key -> expire_at (time.monotonic)
        self._waiters: dict[str, list[asyncio.Future]] = {}

    async def acquire(self, key: str, ttl: float = 30.0) -> bool:
        now = asyncio.get_running_loop().time()
        cur = self._holders.get(key)
        if cur is not None and cur > now:
            return False
        self._holders[key] = now + ttl
        return True

    async def release(self, key: str) -> None:
        self._holders.pop(key, None)
        for fut in self._waiters.pop(key, []):
            if not fut.done():
                fut.set_result(None)

    async def is_locked(self, key: str) -> bool:
        now = asyncio.get_running_loop().time()
        cur = self._holders.get(key)
        if cur is None:
            return False
        if cur <= now:
            self._holders.pop(key, None)
            return False
        return True
