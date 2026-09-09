# -*- coding: utf-8 -*-
"""Kafka Queue（design §5：双队列 run.trigger + run.command）。

生产适配器：kafka-python 生产者/消费者，消息 JSON 序列化。
- publish：producer.send + flush（to_thread 包装）
- subscribe：消费者轮询，异步生成器
本地无 broker 时用 InMemoryQueue（config queue=memory）；故障恢复测试见 M6。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from .base import Queue


class KafkaQueue(Queue):
    def __init__(self, bootstrap: str, *, group_id: str = "agentflow", timeout_ms: int = 1000) -> None:
        self._bootstrap = bootstrap
        self._group_id = group_id
        self._timeout_ms = timeout_ms
        self._producer = None
        # 每个 topic 独立 consumer：双队列（run.trigger / run.command）必须并行消费，
        # 单实例会被第二个 subscribe 覆盖订阅（曾致 command 永远收不到）。
        self._consumers: dict[str, Any] = {}

    # ---- producer ----
    def _ensure_producer(self):
        from kafka import KafkaProducer

        if self._producer is None:
            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap,
                value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if isinstance(k, str) else k,
            )
        return self._producer

    async def publish(self, topic: str, key: str, message: dict) -> None:
        def _send() -> None:
            p = self._ensure_producer()
            p.send(topic, key=key, value=message)
            p.flush()

        await asyncio.to_thread(_send)

    # ---- consumer ----
    def _ensure_consumer(self, topic: str):
        from kafka import KafkaConsumer

        if topic not in self._consumers:
            self._consumers[topic] = KafkaConsumer(
                topic,
                bootstrap_servers=self._bootstrap,
                group_id=self._group_id,
                auto_offset_reset="earliest",
                enable_auto_commit=True,  # at-least-once：重复消费由 §8.4 幂等兜底
                value_deserializer=lambda b: json.loads(b.decode("utf-8")),
            )
        return self._consumers[topic]

    async def subscribe(self, topic: str) -> AsyncIterator[dict]:
        def _poll() -> list[dict]:
            c = self._ensure_consumer(topic)
            raw = c.poll(timeout_ms=self._timeout_ms)
            out: list[dict] = []
            for records in raw.values():
                for r in records:
                    key = r.key.decode() if isinstance(r.key, bytes) else (r.key or "")
                    out.append({"key": key, **r.value})
            return out

        while True:
            msgs = await asyncio.to_thread(_poll)
            for m in msgs:
                yield m
            if not msgs:
                await asyncio.sleep(0.2)  # 无消息时降频轮询
