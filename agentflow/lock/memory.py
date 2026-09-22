"""InMemory Lock（本地测试 / 单进程 MVP）。"""
from __future__ import annotations

import asyncio

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

    async def refresh(self, key: str, ttl: float = 30.0) -> bool:
        """续期；锁还在（未过期）就成功。

        ⚠️ 与 ``RedisLock.refresh`` **语义不同**：内存实现里没有 token 概念，
        它无法校验"是不是同一个调用方"，所以挡不住"已被别人接管"的竞态
        （会替接管者续期）。单进程形态下两者等价 —— 只有一个执行体 ——
        但换实现就是语义差异，写在这里免得被当成同一件事。
        """
        now = asyncio.get_running_loop().time()
        cur = self._holders.get(key)
        if cur is None or cur <= now:
            return False
        self._holders[key] = now + ttl
        return True

    async def is_locked(self, key: str) -> bool:
        now = asyncio.get_running_loop().time()
        cur = self._holders.get(key)
        if cur is None:
            return False
        if cur <= now:
            self._holders.pop(key, None)
            return False
        return True
