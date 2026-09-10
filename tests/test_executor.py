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


async def test_param_resolution_array_index() -> None:
    """数组下标解析：inputs.list[0] 与 nodes 输出的 key_logs[0].msg。"""
    from agentflow.executor.dag_executor import resolve_params

    ctx = {
        "nodes": {"app-log": {"status": "done", "output": {
            "key_logs": [{"level": "ERROR", "msg": "boom", "trace_id": "T1"}],
        }}},
        "inputs": {"correlation_hint": {"sample_trace_ids": ["47239b8d", "a5873ffd"]}},
    }
    p = resolve_params(
        {
            "requestId": "$.inputs.correlation_hint.sample_trace_ids[0]",
            "second": "$.inputs.correlation_hint.sample_trace_ids[1]",
            "msg": "$.nodes.app-log.output.key_logs[0].msg",
            "trace_id": "$.nodes.app-log.output.key_logs[0].trace_id",
        },
        ctx,
    )
    assert p["requestId"] == "47239b8d"
    assert p["second"] == "a5873ffd"
    assert p["msg"] == "boom"
    assert p["trace_id"] == "T1"
    # 越界下标 → None（require 会据此判失败，而非空转）
    p2 = resolve_params({"x": "$.inputs.correlation_hint.sample_trace_ids[9]"}, ctx)
    assert p2["x"] is None


async def test_param_resolution_fallback_priority() -> None:
    """``$.a || $.b`` 回退：前分支有可用值取前分支（新 ticket 顶层 requestId 优先），否则取后分支。"""
    from agentflow.executor.dag_executor import resolve_params

    expr = "$.inputs.requestId || $.inputs.correlation_hint.sample_trace_ids[0]"
    # 新契约：顶层 requestId 存在 → 优先
    ctx_new = {"inputs": {"requestId": "REQ-NEW", "correlation_hint": {"sample_trace_ids": ["OLD"]}}}
    assert resolve_params({"requestId": expr}, ctx_new)["requestId"] == "REQ-NEW"
    # 旧契约：顶层缺失（None）→ 回退 correlation_hint.sample_trace_ids[0]
    ctx_old = {"inputs": {"correlation_hint": {"sample_trace_ids": ["OLD"]}}}
    assert resolve_params({"requestId": expr}, ctx_old)["requestId"] == "OLD"
    # 顶层为空串（require 视为不可用）→ 同样回退
    ctx_empty = {"inputs": {"requestId": "", "correlation_hint": {"sample_trace_ids": ["OLD"]}}}
    assert resolve_params({"requestId": expr}, ctx_empty)["requestId"] == "OLD"
    # 两处皆无 → None（require 判失败，不空转）
    assert resolve_params({"requestId": expr}, {"inputs": {}})["requestId"] is None


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


# ──────────────────────────────────────────────────────────────────
# 入参预检（require）+ 可选墙钟上限（timeout）：输入有问题直接失败，不空转
# ──────────────────────────────────────────────────────────────────

REQUIRE_ABORT_YAML = """
name: require-abort
version: "1.0.0"
inputs: {}
nodes:
  a:
    agent: log-analyst
    require: [requestId]
    params: { requestId: "$.inputs.requestId", bug: "$.inputs.description" }
  b:
    agent: root-cause
edges:
  - { from: a, to: b }
"""


async def test_node_require_missing_aborts_fast() -> None:
    """必填入参 requestId 解析为 null → 立即失败，不调 agent（不空转）→ 整条链 abort。"""
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(REQUIRE_ABORT_YAML)
    called: list[str] = []

    async def runner(node, params):
        called.append(node.id)
        return {"found": True}

    ex = DAGExecutor(
        "run_rq", "t", wf.dag, InMemoryStateStore(), node_runner=runner, inputs={}
    )
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "requestId" in str(ei.value)  # 错误信息指明缺失入参
    assert called == []  # agent 未启动 → 无空转
    assert ex.get_status("a") == "failed"


async def test_node_require_satisfied_runs() -> None:
    """入参可用 → 正常执行直至 done。"""
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(REQUIRE_ABORT_YAML)
    called: list[str] = []

    async def runner(node, params):
        called.append(node.id)
        return {"node": node.id}

    ex = DAGExecutor(
        "run_ok", "t", wf.dag, InMemoryStateStore(), node_runner=runner,
        inputs={"requestId": "abc123", "description": "卡死"},
    )
    outcome = await ex.run()
    assert outcome == "done"
    assert called == ["a", "b"]


async def test_node_require_missing_on_continue_returns_negative_evidence() -> None:
    """on_failure=continue 的节点入参缺失 → 负证据（found:false）而非 abort。"""
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: require-continue
version: "1.0.0"
inputs: {}
nodes:
  diag:
    agent: log-analyst
    require: [requestId]
    on_failure: continue
    params: { requestId: "$.inputs.requestId" }
  fin:
    agent: root-cause
    params: { ev: "$.nodes.diag.output" }
edges:
  - { from: diag, to: fin }
"""
    wf = Workflow.load_yaml(yaml_text)

    async def runner(node, params):
        return {"node": node.id, "params": params}

    ex = DAGExecutor(
        "run_cn", "t", wf.dag, InMemoryStateStore(), node_runner=runner, inputs={}
    )
    outcome = await ex.run()
    assert outcome == "done"
    assert ex.get_status("diag") == DONE
    assert ex.get_status("fin") == DONE
    # diag 产出负证据，fin 收到它
    fin_output = ex.get_output("fin")
    assert fin_output["params"]["ev"]["found"] is False
    assert "requestId" in fin_output["params"]["ev"]["error"]


async def test_node_wallclock_timeout_marks_failed() -> None:
    """node.timeout 墙钟上限：慢 runner 超时 → 节点失败 → 整条链 abort。"""
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: timeout-fail
version: "1.0.0"
inputs: {}
nodes:
  a:
    agent: triage
    timeout: 0.05
  b:
    agent: root-cause
edges:
  - { from: a, to: b }
"""
    wf = Workflow.load_yaml(yaml_text)

    async def slow_runner(node, params):
        await asyncio.sleep(1.0)
        return {"ok": True}

    ex = DAGExecutor("run_tm", "t", wf.dag, InMemoryStateStore(), node_runner=slow_runner)
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "超时" in str(ei.value)
    assert ex.get_status("a") == "failed"


# ──────────────────────────────────────────────────────────────────
# 节点开始即落 running：执行中 GET /runs/{id}（实时读库）能看见它在跑
# ──────────────────────────────────────────────────────────────────
async def test_executing_node_persisted_as_running() -> None:
    """gate runner 进入后阻塞 → store 里该节点 status=='running' 且带解析后 params。

    回归：曾只在内存置 RUNNING，DB 无行 → 前端看不到「执行中」。终态 _persist 覆盖本行。
    """
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: single-running
version: "1.0.0"
inputs:
  bug: { type: string }
nodes:
  a:
    agent: triage
    params: { bug: "$.inputs.bug" }
"""
    wf = Workflow.load_yaml(yaml_text)
    store = InMemoryStateStore()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_runner(node, params):
        entered.set()
        await release.wait()  # 卡在执行中，模拟长 LLM 节点
        return {"node": node.id, "ok": True}

    ex = DAGExecutor(
        "run_rn", "t", wf.dag, store, node_runner=gated_runner, inputs={"bug": "卡死"}
    )
    task = asyncio.create_task(ex.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)  # runner 已进 → running 已落库
        nodes = await store.get_nodes("run_rn")
        assert nodes["a"]["status"] == "running"
        assert nodes["a"]["params"] == {"bug": "卡死"}  # running 行带解析后入参
    finally:
        release.set()
    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome == "done"
    assert ex.get_status("a") == DONE
    # 终态覆盖 running 行，无脏数据
    nodes = await store.get_nodes("run_rn")
    assert nodes["a"]["status"] == "done"
