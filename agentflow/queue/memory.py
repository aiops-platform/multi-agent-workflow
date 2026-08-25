# -*- coding: utf-8 -*-
"""InMemory Queue（本地测试 / 单进程 MVP）。"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any, AsyncIterator

from .base import Queue


class InMemoryQueue(Queue):
    def __init__(self) -> None:
        self._queues: dict[str, deque] = defaultdict(deque)
        self._waiters: dict[str, list[asyncio.Future]] = defaultdict(list)

    async def publish(self, topic: str, key: str, message: dict) -> None:
        payload = {"key": key, **message}
        self._queues[topic].append(payload)
        for fut in self._waiters.get(topic, [])[:]:
            if not fut.done():
                fut.set_result(None)

    async def subscribe(self, topic: str) -> AsyncIterator[dict]:
        while True:
            q = self._queues[topic]
            if q:
                yield q.popleft()
                continue
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters[topic].append(fut)
            try:
                await fut
            finally:
                self._waiters[topic].remove(fut)
