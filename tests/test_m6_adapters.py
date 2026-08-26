# -*- coding: utf-8 -*-
"""M6：生产适配器 —— RedisLock（fakeredis）/ KafkaQueue（mock）/ PostgresStateStore（构造）。

真实 broker/DB 的故障恢复（Kafka 重放、PG 回滚）需生产环境验证（design §14），
本文件验证适配器逻辑与构造正确性。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agentflow.lock.redis import RedisLock
from agentflow.queue.kafka import KafkaQueue


# ======================================================================
# RedisLock（fakeredis 内存实现）
# ======================================================================
@pytest.fixture
def fake_redis():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis()


async def test_redis_lock_acquire_release(fake_redis) -> None:
    lock = RedisLock("redis://x", client=fake_redis)
    assert await lock.acquire("worker-1", ttl=30) is True
    assert await lock.is_locked("worker-1") is True
    await lock.release("worker-1")
    assert await lock.is_locked("worker-1") is False
    await lock.close()


async def test_redis_lock_mutual_exclusion(fake_redis) -> None:
    """两个 worker 抢同一把锁：只有一个成功。"""
    l1 = RedisLock("redis://x", client=fake_redis)
    l2 = RedisLock("redis://x", client=fake_redis)
    assert await l1.acquire("lock:run1") is True
    assert await l2.acquire("lock:run1") is False  # 已被 l1 持有
    await l1.release("lock:run1")
    assert await l2.acquire("lock:run1") is True
    await l2.release("lock:run1")


async def test_redis_lock_ttl_expiry(fake_redis) -> None:
    lock = RedisLock("redis://x", client=fake_redis)
    await lock.acquire("k", ttl=0.2)
    assert await lock.is_locked("k") is True
    await asyncio.sleep(0.3)
    assert await lock.is_locked("k") is False  # TTL 过期自动释放
    await lock.close()


async def test_redis_lock_release_only_own_token(fake_redis) -> None:
    """token 校验：不误删他人持有的锁。"""
    l1 = RedisLock("redis://x", client=fake_redis)
    l2 = RedisLock("redis://x", client=fake_redis)
    await l1.acquire("k")
    await l2.release("k")  # l2 的 token 不匹配 → 不应删除
    assert await l1.is_locked("k") is True
    await l1.release("k")


# ======================================================================
# KafkaQueue（mock producer/consumer 验证消息结构与序列化）
# ======================================================================
class _FakeProducer:
    def __init__(self, **kw) -> None:
        self.sent: list[tuple] = []

    def send(self, topic, *, key=None, value=None):
        self.sent.append((topic, key, value))
        return self

    def flush(self) -> None:
        pass


class _FakeConsumer:
    def __init__(self, *topics, **kw) -> None:
        self.records: list = []
        self.kw = kw

    def poll(self, timeout_ms=1000):
        if not self.records:
            return {}
        r = self.records.pop(0)
        return {0: [r]}


def _patch_kafka(monkeypatch, consumer_records: list | None = None):
    monkeypatch.setattr("kafka.KafkaProducer", _FakeProducer)
    monkeypatch.setattr("kafka.KafkaConsumer", lambda *t, **kw: _FakeConsumer(*t, **kw))
    if consumer_records:
        # 预置消费记录
        import kafka

        orig = kafka.KafkaConsumer

        def factory(*topics, **kw):
            fc = _FakeConsumer(*topics, **kw)
            fc.records = consumer_records
            return fc

        monkeypatch.setattr("kafka.KafkaConsumer", factory)


async def test_kafka_publish_serializes(monkeypatch) -> None:
    _patch_kafka(monkeypatch)
    q = KafkaQueue("localhost:9092")
    await q.publish("run.trigger", "run_1", {"type": "trigger", "run_id": "run_1"})
    topic, key, value = q._producer.sent[0]
    assert topic == "run.trigger"
    assert key == "run_1"
    # 适配器把 dict 交给 KafkaProducer（其 value_serializer 负责序列化成 bytes）
    assert value["run_id"] == "run_1"


async def test_kafka_subscribe_deserializes(monkeypatch) -> None:
    rec = __import__("types").SimpleNamespace(
        key=b"run_1", value={"type": "resume", "run_id": "run_1", "trigger": "approval_timeout"}
    )
    _patch_kafka(monkeypatch, consumer_records=[rec])
    q = KafkaQueue("localhost:9092")
    async for msg in q.subscribe("run.command"):
        assert msg["key"] == "run_1"
        assert msg["type"] == "resume"
        break


# ======================================================================
# PostgresStateStore（构造正确性；真实 PG 验证见 §14 生产专项）
# ======================================================================
async def test_postgres_state_store_construct() -> None:
    from agentflow.statestore import build_state_store

    store = build_state_store(_SettingsStub())
    assert store.__class__.__name__ == "PostgresStateStore"
    assert store._dsn.startswith("postgresql://")


class _SettingsStub:
    state_store = "postgres"
    postgres_dsn = "localhost:5432/agentflow?user=agentflow&password=agentflow"
