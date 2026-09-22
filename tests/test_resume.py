"""M2：断点续跑 + 审批恢复（design §4.4 / §8.5 版本冻结）。"""
from __future__ import annotations

import asyncio

from agentflow.core.dag import DONE, WAITING_APPROVAL
from agentflow.executor.dag_executor import DAGExecutor
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


#: 门之后**必定 abort** 的图：`boom` 的 `require` 拿不到键 → 入参预检失败 →
#: `on_failure: abort` → 抛 `WorkflowNodeFailed`。用来钉住"节点 abort 之后 run 行的状态"。
GATE_THEN_ABORT_YAML = """
name: gate-then-abort
inputs: {}
nodes:
  approve:
    kind: approval
    approvers: ["lead"]
    timeout: 3600
    on_reject: continue
  boom:
    agent: triage
    require: [must_have]
    params: { bug: "$.inputs.bug_report" }
    on_failure: abort
edges:
  - { from: approve, to: boom }
"""


#: 只有"必定 abort 的节点"的图（无门）—— 给 `create_run` 那条同步路径用。
#: ⚠️ 不要用 `kind: halt` 代替门：halt 一执行就把其余 PENDING 全部 SKIPPED（§3.1），
#: 那个 abort 节点压根不会跑，run 会是 `done` —— 测的就不是这件事了。
ABORT_ONLY_YAML = """
name: abort-only
inputs: {}
nodes:
  boom:
    agent: triage
    require: [must_have]
    params: { bug: "$.inputs.bug_report" }
    on_failure: abort
edges: []
"""


async def test_approve_marks_run_failed_when_a_later_node_aborts() -> None:
    """审批后下游节点 abort → run 行必须是 `failed`，**不能停在 `waiting_approval`**。

    回归（run_4caefc4cd8 实测）：`approve` 的收尾原先是
    `outcome = await ex.run(); await store.update_run(run_id, status=outcome)` ——
    `ex.run()` 抛 `WorkflowNodeFailed` 时下面那行**永远不执行**，于是"节点已 failed、
    run 还写着等待审批"，而 `updated_at` 停在审批那一刻、**没有人会再来改它**：
    页面上表现为这条 run 一直在等审批。四处 `ex.run()` 收尾里只有 `_run_background` 有兜底。
    """
    from agentflow.core.workflow import Workflow
    from agentflow.service import RunService

    wf = Workflow.load_yaml(GATE_THEN_ABORT_YAML)
    store = await _setup_db()
    svc = RunService(store)
    summary = await svc.create_run("tenant-a", wf, {})
    run_id = summary["run_id"]
    assert (await store.get_run(run_id))["status"] == WAITING_APPROVAL

    res = await svc.approve(run_id, "approve", approved=True, by="lead")

    assert res["run_status"] == "failed"
    assert (await store.get_run(run_id))["status"] == "failed"  # ← 回归点：不是 waiting_approval


async def test_create_run_marks_run_failed_when_a_node_aborts() -> None:
    """同一条收尾的另一处（`create_run`，同步变体）：节点 abort 也要落 `failed`。

    与上一条共用一个 helper —— 分开写就会像这次一样漂移一半，而漂移的那一半**没有提示**。
    """
    from agentflow.core.workflow import Workflow
    from agentflow.service import RunService

    wf = Workflow.load_yaml(ABORT_ONLY_YAML)
    store = await _setup_db()
    svc = RunService(store)
    summary = await svc.create_run("tenant-a", wf, {})

    assert (await store.get_run(summary["run_id"]))["status"] == "failed"


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
