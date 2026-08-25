# -*- coding: utf-8 -*-
"""Kafka Queue（M6 生产适配器，当前为占位实现）。

design §14 风险：Kafka 重放 / 消费组偏移是 M6 故障恢复专项测试范围。
本占位保证 import 可用、publish 抛出未实现错误，避免误用。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .base import Queue


class KafkaQueue(Queue):
    def __init__(self, bootstrap: str) -> None:
        self._bootstrap = bootstrap

    async def publish(self, topic: str, key: str, message: dict) -> None:
        raise NotImplementedError("KafkaQueue 是 M6 适配器，尚未实现")

    async def subscribe(self, topic: str) -> AsyncIterator[dict]:
        raise NotImplementedError("KafkaQueue 是 M6 适配器，尚未实现")
        yield  # pragma: no cover
