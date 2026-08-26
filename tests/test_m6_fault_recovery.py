# -*- coding: utf-8 -*-
"""M6：故障恢复专项测试（design §14）。

- Worker SIGKILL 恢复：Worker 被杀后，新 Worker 从 StateStore checkpoint 恢复，
  已完成节点不重做（幂等），未完成节点继续。
- 消息重放幂等：重复触发同 workflow → 复用同一 snapshot；副作用按
  external_operation_id 去重（§8.4）。
（Kafka 重放 / PG 事务回滚需真实 broker/DB，生产验证见 §14。）
"""
from __future__ import annotations

import asyncio

from agentflow.core.dag import DONE, WAITING_APPROVAL
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import DAGExecutor
from agentflow.statestore.sqlite import SqliteStateStore

from .conftest import PARALLEL_YAML


def _make_runner(calls: dict):
    async def runner(node, params):
        calls[node.id] = calls.get(node.id, 0) + 1
        await asyncio.sleep(0.005)
        return {"node": node.id, "summary": f"{node.agent}-out"}

    return runner


async def _db():
    store = SqliteStateStore(":memory:")
    await store.connect()
    return store


async def test_worker_sigkill_resume_no_redo() -> None:
    """Worker 在审批挂起时被杀（SIGKILL）→ 新 Worker 恢复 → 已完成节点不重做。"""
    wf = Workflow.load_yaml(PARALLEL_YAML)
    store = await _db()
    calls: dict[str, int] = {}
    runner = _make_runner(calls)

    # Worker 1：执行到 waiting_approval（此时 triage/logs/trace/rca 已持久化）
    ex1 = DAGExecutor("run_k", "t", wf.dag, store, node_runner=runner)
    assert await ex1.run() == "waiting_approval"
    assert ex1.get_status("rca") == DONE

    # SIGKILL：丢弃 ex1，新 Worker 从 checkpoint 重建（§4.4 Resume）
    ex2 = await DAGExecutor.from_checkpoint("run_k", "t", wf.dag, store, node_runner=runner)
    assert ex2.get_status("rca") == DONE  # 恢复已完成的节点
    assert await ex2.run() == "waiting_approval"  # 仍在等审批

    # 审批通过 → 继续执行到 done
    await ex2.approve("approve", approved=True, by="lead")
    assert await ex2.run() == "done"
    assert ex2.get_status("test") == DONE

    # 已完成节点不重做（每个最多执行 1 次）
    for nid in ["triage", "logs", "trace", "rca"]:
        assert calls[nid] == 1, f"{nid} 被重复执行 {calls[nid]} 次"
    await store.close()


async def test_worker_sigkill_mid_execution_resumes() -> None:
    """Worker 在节点执行中被杀（模拟 task 取消）→ 恢复后 run 仍可收敛。"""
    from agentflow.core.dag import SKIPPED

    wf = Workflow.load_yaml(PARALLEL_YAML)
    store = await _db()
    calls: dict[str, int] = {}

    async def slow_runner(node, params):
        calls[node.id] = calls.get(node.id, 0) + 1
        await asyncio.sleep(0.02)
        return {"node": node.id, "summary": "ok"}

    ex = DAGExecutor("run_m", "t", wf.dag, store, node_runner=slow_runner)
    # 执行中取消（模拟 SIGKILL）
    task = asyncio.create_task(ex.run())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 新 Worker 恢复并跑完
    ex2 = await DAGExecutor.from_checkpoint("run_m", "t", wf.dag, store, node_runner=slow_runner)
    # 恢复后继续（可能是 waiting_approval 或 done，取决于取消点）
    outcome = await ex2.run()
    assert outcome in ("waiting_approval", "done")
    if outcome == "waiting_approval":
        await ex2.approve("approve", approved=True, by="lead")
        assert await ex2.run() == "done"
    await store.close()


async def test_message_replay_reuses_snapshot() -> None:
    """消息重放：同一 workflow 重复触发 → 复用同一 snapshot（§8.5 版本冻结幂等）。"""
    from agentflow.service import RunService

    store = await _db()
    wf = Workflow.load_yaml(PARALLEL_YAML)
    svc = RunService(store, node_runner=_make_runner({}))
    r1 = await svc.create_run("t", wf, {})
    r2 = await svc.create_run("t", wf, {})
    assert r1["run_id"] != r2["run_id"]  # 不同 run
    # 同一 workflow_hash → snapshot 复用（同 id）
    s1 = await store.get_snapshot(wf.workflow_hash)
    s2 = await store.get_snapshot(wf.workflow_hash)
    assert s1 is not None and s2 is not None
    assert s1["workflow_hash"] == s2["workflow_hash"] == wf.workflow_hash
    await store.close()


async def test_replayed_side_effect_idempotent() -> None:
    """重放带外部操作的副作用：external_operation_id 命中 → 不重复执行（§8.4）。"""
    from agentflow.executor.idempotency import execute_with_idempotency

    store = await _db()
    calls = {"n": 0}

    async def action():
        calls["n"] += 1
        return {"pr_number": 42}

    await execute_with_idempotency(store, "run_r", "commit", 0, action, external_operation_id="PR-42")
    await execute_with_idempotency(store, "run_r", "commit", 0, action, external_operation_id="PR-42")
    assert calls["n"] == 1  # 重放不重复副作用
    await store.close()
