# -*- coding: utf-8 -*-
"""M2：并发 DAG 执行 + join/skip + 审批（design §8.2 / §8.3 / §8.6）。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentflow.core.dag import DONE, REJECTED, SKIPPED, WAITING_APPROVAL
from agentflow.executor.dag_executor import DAGExecutor, WorkflowNodeFailed
from agentflow.statestore.memory import InMemoryStateStore

from .conftest import PARALLEL_YAML, SIMPLE_YAML


def make_runner(calls: dict | None = None):
    """确定性 runner：按 agent 名返回结构化输出，记录调用次数。"""
    state = calls if calls is not None else {}

    async def runner(node, params):
        state[node.id] = state.get(node.id, 0) + 1
        out = {"node": node.id, "params": params}
        if node.agent == "triage":
            out.update(summary=f"triage-{node.id}", symptom_type="hang")
        elif node.agent == "approval":  # pragma: no cover - 审批不走 runner
            pass
        else:
            out.update(summary=f"{node.agent}-out")
        return out

    return runner, state


def build_executor(yaml_text: str):
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(yaml_text)
    runner, calls = make_runner()
    store = InMemoryStateStore()
    ex = DAGExecutor("run_1", "tenant-a", wf.dag, store, node_runner=runner)
    return ex, wf, store, calls


async def test_simple_chain_done() -> None:
    ex, _, store, calls = build_executor(SIMPLE_YAML)
    outcome = await ex.run()
    assert outcome == "done"
    assert all(ex.get_status(nid) == DONE for nid in ["a", "b", "c"])
    # 参数解析：a 的 params.bug 来自 inputs；b 的 params.bug 来自 a.output.summary
    assert calls["a"] == 1 and calls["b"] == 1 and calls["c"] == 1
    # checkpoint 已落盘
    nodes = await store.get_nodes("run_1")
    assert set(nodes) == {"a", "b", "c"}


async def test_param_resolution_output_accessor() -> None:
    """$．nodes.X.output（无字段）与 .output.field 都应正确解析（回归：曾把 output 当字段遍历返回 None）。"""
    from agentflow.executor.dag_executor import resolve_params

    ctx = {"nodes": {"fix": {"status": "done", "output": {"diff": "--- a\n+++ b", "files": ["W.java"]}}}, "inputs": {}}
    p = resolve_params({"whole": "$.nodes.fix.output", "diff": "$.nodes.fix.output.diff"}, ctx)
    assert p["whole"]["diff"] == "--- a\n+++ b"  # 整输出可解析
    assert p["diff"] == "--- a\n+++ b"  # 字段可解析


async def test_parallel_with_approval_waiting() -> None:
    ex, _, store, calls = build_executor(PARALLEL_YAML)
    outcome = await ex.run()
    assert outcome == "waiting_approval"
    assert ex.get_status("approve") == WAITING_APPROVAL
    # rca 已完成（join: all 依赖 logs+trace 都完成）
    assert ex.get_status("rca") == DONE
    assert ex.get_status("logs") == DONE and ex.get_status("trace") == DONE


async def test_approval_approve_continues_and_skip_sibling() -> None:
    ex, _, _, calls = build_executor(PARALLEL_YAML)
    await ex.run()
    await ex.approve("approve", approved=True, by="lead")
    outcome = await ex.run()
    assert outcome == "done"
    assert ex.get_status("test") == DONE  # 条件满足 → 执行
    assert ex.get_status("recap") == SKIPPED  # 兄弟分支条件不满足 → skip 级联


async def test_approval_reject_routes_to_recap() -> None:
    ex, _, _, calls = build_executor(PARALLEL_YAML)
    await ex.run()
    out = await ex.approve("approve", approved=False, by="lead", comment="方案不合规")
    assert out["approved"] is False
    outcome = await ex.run()
    assert outcome == "done"
    assert ex.get_status("test") == SKIPPED
    assert ex.get_status("recap") == DONE


async def test_approval_cas_prevents_double_approve() -> None:
    from agentflow.executor.dag_executor import ApprovalRaceError

    ex, _, _, calls = build_executor(PARALLEL_YAML)
    await ex.run()
    await ex.approve("approve", approved=True)
    # 终态不可逆（§8.3）：再次审批直接断言失败
    with pytest.raises(AssertionError):
        await ex.approve("approve", approved=False)


async def test_approval_node_skipped_when_condition_false() -> None:
    """S-010b：approval 的 when 不满足 → SKIPPED 而非 WAITING。"""
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: approval-skip
version: "1.0.0"
inputs: {}
nodes:
  diag:
    agent: triage
    params: { flag: false }
  approve:
    kind: approval
    params: { name: "仅当 flag 时才审批" }
    when: "$.nodes.diag.output.flag == true"
  finish:
    agent: postmortem
    params: { rca: "$.nodes.diag.output" }
edges:
  - { from: diag, to: approve }
  - { from: diag, to: finish }
  - { from: approve, to: finish, when: "$.nodes.approve.output.approved == true" }
"""
    wf = Workflow.load_yaml(yaml_text)
    runner, calls = make_runner()

    async def runner_flag(node, params):
        return {"flag": False, "summary": "diag"}

    ex = DAGExecutor("run_s", "t", wf.dag, InMemoryStateStore(), node_runner=runner_flag)
    outcome = await ex.run()
    assert outcome == "done"  # 无审批等待
    assert ex.get_status("approve") == SKIPPED  # when 不满足 → skip，非 WAITING
    assert ex.get_status("finish") == DONE


async def test_node_failure_aborts_run() -> None:
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: fail
version: "1.0.0"
inputs: {}
nodes:
  a:
    agent: triage
    retry: 0
  b:
    agent: root-cause
edges:
  - { from: a, to: b }
"""
    wf = Workflow.load_yaml(yaml_text)

    async def failing_runner(node, params):
        raise RuntimeError("boom")

    ex = DAGExecutor("run_f", "t", wf.dag, InMemoryStateStore(), node_runner=failing_runner)
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
