# -*- coding: utf-8 -*-
"""Queue 适配层：配置驱动切换（design §5）。"""
from __future__ import annotations

from ..config import Settings
from .base import Queue
from .memory import InMemoryQueue


def build_queue(settings: Settings) -> Queue:
    if settings.queue == "memory":
        return InMemoryQueue()
    if settings.queue == "kafka":
        from .kafka import KafkaQueue

        return KafkaQueue(settings.kafka_bootstrap)
    raise ValueError(f"未知 queue 后端: {settings.queue!r}")
