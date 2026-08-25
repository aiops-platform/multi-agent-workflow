# -*- coding: utf-8 -*-
"""Redis Lock（M6 生产适配器，占位）。"""
from __future__ import annotations

from .base import Lock


class RedisLock(Lock):
    def __init__(self, url: str) -> None:
        self._url = url

    async def acquire(self, key: str, ttl: float = 30.0) -> bool:
        raise NotImplementedError("RedisLock 是 M6 适配器，尚未实现")

    async def release(self, key: str) -> None:
        raise NotImplementedError("RedisLock 是 M6 适配器，尚未实现")

    async def is_locked(self, key: str) -> bool:
        raise NotImplementedError("RedisLock 是 M6 适配器，尚未实现")
