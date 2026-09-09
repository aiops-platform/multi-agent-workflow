"""M2：副作用幂等（design §8.4 execution_id + external_operation_id）。"""
from __future__ import annotations

from agentflow.core.dag import DAG
from agentflow.executor.dag_executor import DAGExecutor
from agentflow.executor.idempotency import execute_with_idempotency
from agentflow.statestore.memory import InMemoryStateStore
from agentflow.statestore.sqlite import SqliteStateStore


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


# ======================================================================
# executor 接线（§8.4.2 真实 DAG 路径）
# ======================================================================
# 此前 dag_executor 调 execute_with_idempotency 从不传 external_operation_id
# （恒 None）→ 复用分支在真实执行/Resume 中永不触发，SIGKILL 重放会重复建 PR /
# 重复 scale。以下测试验证接线后的真实执行路径。

async def test_side_effect_node_crash_replay_reuses_attempt_sqlite() -> None:
    """§8.4.2 接线回归：committer（副作用 agent）执行成功后节点 checkpoint 丢失
    （模拟 crash 发生在 record_attempt 与 put_node 之间），同 run 重放 →
    不重跑 runner，复用成功记录的输出，副作用只发生一次。"""
    store = SqliteStateStore(":memory:")
    await store.connect()
    dag = DAG.build(
        {"commit": {"agent": "committer", "params": {"diff": "$.inputs.diff"}}}, []
    )
    calls = {"n": 0}

    async def runner(node, params):
        calls["n"] += 1
        return {"pr_number": 7}

    ex = DAGExecutor("run_x", "t", dag, store, node_runner=runner, inputs={"diff": "d"})
    assert await ex.run() == "done"
    assert calls["n"] == 1

    # crash 窗口：attempt 记录已在（succeeded），节点 checkpoint 回退 pending
    await store.put_node("run_x", "t", "commit", {"status": "pending", "output": None})
    assert await store.get_succeeded_attempt("run_x", "commit", "run_x:commit") is not None

    ex2 = DAGExecutor("run_x", "t", dag, store, node_runner=runner, inputs={"diff": "d"})
    assert await ex2.run() == "done"
    assert calls["n"] == 1  # 幂等复用，未重复执行副作用
    assert ex2.get_output("commit") == {"pr_number": 7}
    await store.close()


async def test_declared_idempotency_key_resolved_from_inputs() -> None:
    """YAML ``idempotency_key`` 支持 $. 引用（§8.4.3：repo+base_sha 等内容键），
    解析值作为 external_operation_id 落 node_attempts。"""
    store = InMemoryStateStore()
    dag = DAG.build(
        {"commit": {"agent": "committer", "idempotency_key": "$.inputs.base_sha"}}, []
    )

    async def runner(node, params):
        return {"pr_number": 1}

    ex = DAGExecutor("run_y", "t", dag, store, node_runner=runner, inputs={"base_sha": "abc123"})
    assert await ex.run() == "done"
    att = await store.get_succeeded_attempt("run_y", "commit", "abc123")
    assert att is not None and att["status"] == "succeeded"


async def test_non_side_effect_node_has_no_idempotency_key() -> None:
    """只读节点（诊断侧）不产生幂等键——不做跨 crash 复用，crash 后重新取证。"""
    store = InMemoryStateStore()
    dag = DAG.build({"logs": {"agent": "log-analyst"}}, [])

    async def runner(node, params):
        return {"found": True}

    ex = DAGExecutor("run_z", "t", dag, store, node_runner=runner)
    assert await ex.run() == "done"
    assert await store.get_succeeded_attempt("run_z", "logs", "run_z:logs") is None
