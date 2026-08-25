# -*- coding: utf-8 -*-
"""Lock 适配层：配置驱动切换（design §5）。"""
from __future__ import annotations

from ..config import Settings
from .base import Lock
from .memory import InMemoryLock


def build_lock(settings: Settings) -> Lock:
    if settings.lock == "memory":
        return InMemoryLock()
    if settings.lock == "redis":
        from .redis import RedisLock

        return RedisLock(settings.redis_url)
    raise ValueError(f"未知 lock 后端: {settings.lock!r}")
