"""Problem Center「分析new」workflow（scripts/problem-log-diagnose.workflow.yaml）。

锁三件事：
1. **能加载**——即已过 `core/dag.py` 的静态校验（无环 / join 一致性 / params 引用只指上游）；
2. **拓扑合理**——日志证据先于根因、审批门等齐 rca+plan、拒绝路径有且只有那条 when 边；
3. **行为**——批准路径 run `success` 且 recap 被 skip；拒绝路径 recap 执行（引擎忽略 `on_reject`，
   拒绝路由完全依赖那条 `when` 边，这里就是它的回归测试）。

用默认 mock runner（`DAGExecutor` 自带），不调 LLM、不连 MCP——跑得快且确定性。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow.core.dag import DONE, SKIPPED
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import DAGExecutor, WorkflowNodeFailed
from agentflow.statestore.memory import InMemoryStateStore

WF_PATH = Path(__file__).resolve().parent.parent / "scripts" / "problem-log-diagnose.workflow.yaml"

INPUTS = {
    "bug_report": {
        "number": "PR-0001",
        "short_description": "订单服务结账无响应",
        "description": "log[ERROR] java.lang.NullPointerException x13",
        "cmdb_ci": {"name": "order-service", "namespace": "order"},
        "symptom": {"summary": "Cannot invoke String.trim() because template is null x13"},
        "requestId": "3535d49a-ec62-49b2-9da5-a9f9d4ab8a28",
    },
    "window_start": "2026-09-17T07:37:20+00:00",
    "window_end": "2026-09-17T09:14:54+00:00",
}


def load() -> Workflow:
    return Workflow.load_yaml(WF_PATH)


def build(store=None, inputs=None) -> DAGExecutor:
    wf = load()
    return DAGExecutor(
        "run_log", "otr", wf.dag, store or InMemoryStateStore(), inputs=inputs or dict(INPUTS)
    )


# ── 结构守卫 ────────────────────────────────────────────────────────────────


def test_workflow_loads_and_has_expected_nodes() -> None:
    dag = load().dag
    # `know` 已移除：占位工具 search_knowledge 恒返回 INC0001，一次真实 run 却调了 5 次，
    # 纯开销。接真实知识库后加回，届时同步本断言与 rca.required_edges。
    assert set(dag.nodes) == {
        "triage", "logs", "locate", "rca", "plan", "approve-plan", "recap",
    }
    assert dag.nodes["approve-plan"].is_approval


def test_plan_uses_multi_option_analyst_with_matching_param_name() -> None:
    """plan 用 remediation-planning-analyst 才有互斥多方案（fix-planner 只出单计划）。

    入参名必须与 agent 对得上——它只读 `root_cause`；沿用旧的 `rca` 会让 require
    静默放过 None（预检按同名 key 查），agent 拿到空输入却不报错。
    """
    node = load().dag.nodes["plan"]
    assert node.agent == "remediation-planning-analyst"
    assert set(node.require) == {"root_cause"}
    assert "root_cause" in (node.params or {})


def test_logs_node_is_the_only_data_fetcher_with_window_require() -> None:
    """日志是唯一取数节点，且强制要时间窗（MCP 侧 start_time/end_time 必填）。"""
    node = load().dag.nodes["logs"]
    assert node.agent == "log-analyst"
    assert set(node.require) == {"service", "start_time", "end_time"}
    assert node.retry == 1


def test_rca_waits_for_all_evidence_sources() -> None:
    """`join: all` + required_edges 必须列全部直接上游，否则根因会在日志之前被调度。"""
    dag = load().dag
    rca = dag.nodes["rca"]
    assert rca.join == "all"
    assert set(rca.required_edges) == {"triage", "logs", "locate"}
    assert {e.source for e in rca.in_edges} == set(rca.required_edges)


def test_approval_gate_waits_for_rca_and_plan() -> None:
    """审批门必须等齐 rca 与 plan（卡片画的是 upstream：两条都要在）。"""
    node = load().dag.nodes["approve-plan"]
    assert node.join == "all"
    assert set(node.required_edges) == {"rca", "plan"}


def test_only_reject_edge_reaches_recap() -> None:
    """拒绝/超时 → recap 的那条 when 边是**唯一**入口（引擎忽略 on_reject）。"""
    dag = load().dag
    into_recap = dag.nodes["recap"].in_edges
    assert len(into_recap) == 1
    edge = into_recap[0]
    assert edge.source == "approve-plan"
    assert edge.when == "$.nodes.approve-plan.output.approved == false"


# ── 行为：批准 / 拒绝 ────────────────────────────────────────────────────────


async def test_run_waits_at_approval_then_approve_finishes_success() -> None:
    ex = build()
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == ["approve-plan"]
    assert ex.get_status("logs") == DONE
    assert ex.get_status("recap") == "pending"  # 尚未决定

    await ex.approve("approve-plan", approved=True, by="lead-engineer")
    assert await ex.run() == "done"
    assert ex.get_status("approve-plan") == DONE
    assert ex.get_status("recap") == SKIPPED


async def test_reject_routes_to_recap() -> None:
    ex = build()
    await ex.run()
    out = await ex.approve("approve-plan", approved=False, by="lead-engineer", comment="方案不对")
    assert out["approved"] is False
    assert await ex.run() == "done"
    assert ex.get_status("recap") == DONE


async def test_missing_window_fails_fast_at_logs() -> None:
    """缺时间窗 → logs 的 require 拦住（不空转），on_failure=abort → 整条 run 失败。

    这是刻意的：日志是本流程唯一证据源，拿不到就不出计划。
    """
    inputs = dict(INPUTS)
    inputs.pop("window_start")
    ex = build(inputs=inputs)
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "start_time" in str(ei.value)
    assert ex.get_status("logs") == "failed"
    assert ex.get_status("rca") == "pending"  # 下游没跑


async def test_upstream_service_missing_fails_fast() -> None:
    """工单没有服务名（cmdb_ci.name 为空）→ logs 的 require 同样拦住。"""
    inputs = {
        "bug_report": {"number": "PR-0002", "short_description": "x", "cmdb_ci": {"name": ""}},
        "window_start": INPUTS["window_start"],
        "window_end": INPUTS["window_end"],
    }
    ex = build(inputs=inputs)
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "service" in str(ei.value)
