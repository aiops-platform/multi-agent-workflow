# -*- coding: utf-8 -*-
"""M2：断点续跑 + 审批恢复（design §4.4 / §8.5 版本冻结）。"""
from __future__ import annotations

import asyncio
from typing import Any

from agentflow.core.dag import DONE, WAITING_APPROVAL
from agentflow.executor.dag_executor import DAGExecutor
from agentflow.statestore.memory import InMemoryStateStore
from agentflow.statestore.sqlite import SqliteStateStore

from .conftest import PARALLEL_YAML


async def make_runner(calls: dict):
    async def runner(node, params):
        await asyncio.sleep(0.005)
        calls[node.id] = calls.get(node.id, 0) + 1
        return {"node": node.id, "summary": f"{node.agent}-out"}

    return runner


async def _setup_db():
    store = SqliteStateStore(":memory:")
    await store.connect()
    return store


async def test_resume_after_approval_sqlite() -> None:
    """S-010b 核心：审批挂起 → 新 executor 从 checkpoint 恢复 → 审批 → 继续。"""
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(PARALLEL_YAML)
    store = await _setup_db()
    calls: dict[str, int] = {}
    runner = await make_runner(calls)

    # 1) 第一轮：执行到 waiting_approval（模拟 Worker 释放）
    ex1 = DAGExecutor("run_r", "tenant-a", wf.dag, store, node_runner=runner)
    assert await ex1.run() == "waiting_approval"
    assert ex1.get_status("approve") == WAITING_APPROVAL

    # 2) 断点续跑：新 executor 从 checkpoint 恢复（模拟 crash 后重新拉起 Worker）
    ex2 = await DAGExecutor.from_checkpoint("run_r", "tenant-a", wf.dag, store, node_runner=runner)
    # 已完成的节点不重跑
    assert ex2.get_status("rca") == DONE
    assert ex2.get_status("approve") == WAITING_APPROVAL  # 审批状态不自动通过、不回退
    assert await ex2.run() == "waiting_approval"  # 仍是等待审批

    # 3) 审批 → 继续执行到 done
    await ex2.approve("approve", approved=True, by="lead")
    assert await ex2.run() == "done"
    assert ex2.get_status("test") == DONE

    # 4) 幂等：done 节点不重复执行（S-010b 双 crash 恢复语义）
    assert calls["rca"] == 1
    assert calls["test"] == 1


async def test_run_service_create_approve_resume() -> None:
    """RunService 端到端：create_run → approve → done。"""
    from agentflow.core.workflow import Workflow
    from agentflow.service import RunService

    wf = Workflow.load_yaml(PARALLEL_YAML)
    store = await _setup_db()
    svc = RunService(store)
    summary = await svc.create_run("tenant-a", wf, {})
    assert summary["pending_approvals"] == ["approve"]

    res = await svc.approve(summary["run_id"], "approve", approved=True, by="lead")
    assert res["run_status"] == "done"
    assert res["status"]["test"] == DONE
    assert res["status"]["recap"] == "skipped"
