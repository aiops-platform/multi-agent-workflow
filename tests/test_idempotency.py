# -*- coding: utf-8 -*-
"""M2：副作用幂等（design §8.4 execution_id + external_operation_id）。"""
from __future__ import annotations

from agentflow.executor.idempotency import execute_with_idempotency
from agentflow.statestore.memory import InMemoryStateStore


async def test_external_operation_reused() -> None:
    """§8.4.2：同 external_operation_id 的成功记录 → 复用结果，副作用只发生一次。"""
    store = InMemoryStateStore()
    calls = {"n": 0}

    async def action():
        calls["n"] += 1
        return {"pr_number": 42}

    out1 = await execute_with_idempotency(
        store, "run_1", "commit", 0, action, external_operation_id="PR-42"
    )
    out2 = await execute_with_idempotency(
        store, "run_1", "commit", 0, action, external_operation_id="PR-42"
    )
    assert out1 == out2 == {"pr_number": 42}
    assert calls["n"] == 1  # 第二次直接复用，未重跑副作用


async def test_retry_until_success() -> None:
    store = InMemoryStateStore()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    out = await execute_with_idempotency(store, "run_2", "fix", 0, flaky, max_attempts=3)
    assert out == "ok"
    assert calls["n"] == 3
    # 最后一次 attempt 记录为 succeeded（execution_id 含 UUID）
    last_exec_id = list(store._attempts)[-1]
    attempts = await store.get_attempt_by_execution_id(last_exec_id)
    assert attempts["status"] == "succeeded"


async def test_retry_exhausted_raises() -> None:
    import pytest

    store = InMemoryStateStore()

    async def always_fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await execute_with_idempotency(store, "run_3", "fix", 0, always_fail, max_attempts=2)


async def test_on_error_negative_evidence() -> None:
    """on_failure: continue 的负证据路径（§8.1 诊断侧策略）。"""
    store = InMemoryStateStore()

    async def always_fail():
        raise RuntimeError("logs 查不到")

    async def on_error(exc):
        return {"found": False, "error": str(exc)}

    out = await execute_with_idempotency(
        store, "run_4", "logs", 0, always_fail, max_attempts=1, on_error=on_error
    )
    assert out == {"found": False, "error": "logs 查不到"}
