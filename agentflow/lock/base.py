# -*- coding: utf-8 -*-
"""Lock 接口（design §5：分布式锁，审批 CAS / Worker 抢占用）。"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class Lock(ABC):
    @abstractmethod
    async def acquire(self, key: str, ttl: float = 30.0) -> bool:
        """非阻塞获取，成功返回 True。"""

    @abstractmethod
    async def release(self, key: str) -> None: ...

    @abstractmethod
    async def is_locked(self, key: str) -> bool: ...

    async def __aenter__(self) -> "Lock":
        # 子类可覆写；这里仅作为文档性的默认（blocking acquire 由调用方处理）
        return self

    async def __aexit__(self, *exc) -> None:
        pass
