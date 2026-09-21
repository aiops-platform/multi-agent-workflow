"""Problem Center「分析new」workflow（scripts/problem-log-diagnose.workflow.yaml）。

锁四件事：
1. **能加载**——即已过 `core/dag.py` 的静态校验（无环 / join 一致性 / params 引用只指上游）；
2. **拓扑合理**——日志证据先于根因、两道人工门各自等齐它的上游、两条中断边存在；
3. **两道门都是 `continue`**——驳回沿 `approved == false → recap` 边路由、**不中止 run**
   （人工决策不是执行失败，run 仍判 done，由 recap 留痕）；
4. **halt 的裁决权在 executor**——触发后其余 PENDING 节点全部 SKIPPED，不靠逐条 gate 边。

⚠️ 历史（两次修正，方向相反，值得留）：

本文件原有一条 `test_only_reject_edge_reaches_recap`，断言"引擎忽略 `on_reject`、拒绝路由完全
依赖那条 when 边"。**引擎补上消费方之后那条断言就错了**——`on_reject: abort` 真会中止 run
（`dag_executor.py:321 rejected_abort_node()` + `:723`），那时 `approve-plan → recap` 边
**永远不可达**（CLAUDE.md §4.1：「图在骗人，但加载与运行都不报错」），而测试一直在锁这个错行为。

**修法二选一，两边都自洽**：改 `abort` + 删边，或改 `continue` + 留边。本项目最后选的是后者
（上游 `origin/main`，与 `_check_on_reject_consistency` 那条加载期校验同期落地）——
理由是"驳回"是**人的决定**，把它报成 run `failed` 会与"执行出错"混为一谈
（`dag_executor.py:722` 的注释也在强调这个区分）。
故本文件现在锁的是 `continue` 语义。**别只改一边**——`abort` + 驳回边会被
`_check_on_reject_consistency` 在加载期直接拒掉，而 `continue` + 没有驳回边**是合法的**
（反方向不查），那种图会静默变成"驳回后什么都不记"，只能靠测试守住。

用脚本化 runner（不调 LLM、不连 MCP），跑得快且确定性。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow.core.dag import DONE, FAILED, REJECTED, SKIPPED
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

#: 各节点的"正常路径"输出。键名与各 agent 的 schema 对齐——**这很重要**：
#: 默认 mock runner 返回 `{"node": ..., "ok": True}`，任何 `when: $.nodes.X.output.passed == true`
#: 的条件边都会因取不到字段而不满足，下游被静默 SKIPPED（第一次跑就踩到了）。
OK_OUTPUTS: dict[str, dict] = {
    "triage": {"summary": "NPE in checkout", "symptom_type": "crash", "severity": "high"},
    "logs": {"found": True, "summary": "NullPointerException x13"},
    "locate": {
        "found": True,
        "service": "order-service",
        "repo_url": "file:///repos/order-service",
        "summary": "命中 QuotationService",
    },
    "rca": {
        "insufficient": False,
        "confidence": 0.82,
        "summary": "template 为 null 时未校验",
        "hypotheses": ["fin 参数缺失"],
        "ruled_out": ["基础设施"],
    },
    "plan": {"summary": "补空值校验", "steps": [{"type": "code_fix", "target": "QuotationService.java"}]},
    "fix": {"diff": "--- a/Q.java\n+++ b/Q.java\n", "files_changed": ["Q.java"], "explanation": "加空值校验"},
    "test": {"passed": True, "tests_run": 3, "failed": []},
    "review": {"approved": True, "comments": [], "risk": "low"},
    "commit": {"pr_url": None, "pr_number": 0, "base_sha": "abc123"},
    "recap": {"summary": "复盘"},
}


def load() -> Workflow:
    return Workflow.load_yaml(WF_PATH)


def build(outputs: dict | None = None, inputs=None) -> DAGExecutor:
    """按节点 id 返回脚本化输出；未给出的节点回落到 mock 形态。"""
    table = OK_OUTPUTS if outputs is None else outputs

    async def runner(node, params):
        return table.get(node.id, {"node": node.id, "ok": True})

    wf = load()
    return DAGExecutor(
        "run_log", "otr", wf.dag, InMemoryStateStore(),
        inputs=inputs or dict(INPUTS), node_runner=runner,
    )


# ── 结构守卫 ────────────────────────────────────────────────────────────────


def test_workflow_loads_and_has_expected_nodes() -> None:
    dag = load().dag
    # `know` 已移除：占位工具 search_knowledge 恒返回 INC0001，一次真实 run 却调了 5 次，
    # 纯开销。接真实知识库后加回，届时同步本断言与 rca.required_edges。
    assert set(dag.nodes) == {
        "triage", "logs", "locate", "rca", "plan", "approve-plan",
        "fix", "test", "review", "approve-commit", "commit", "halt", "recap",
    }
    assert dag.nodes["approve-plan"].is_approval
    assert dag.nodes["approve-commit"].is_approval
    assert dag.nodes["halt"].is_halt


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


def test_plan_gate_waits_for_rca_and_plan() -> None:
    """计划门必须等齐 rca 与 plan（卡片画的是 upstream：两条都要在）。"""
    node = load().dag.nodes["approve-plan"]
    assert node.join == "all"
    assert set(node.required_edges) == {"rca", "plan"}


def test_evidence_gate_waits_for_diff_test_and_review() -> None:
    """证据门必须等齐 fix/test/review 三者，且三者都是**直接**上游。

    审批卡片的 upstream 只取 `node.upstreams`（api/app.py:978 / executor/dag_executor.py:777），
    只连 review 一条的话，审批人看不到 diff 与测试结果——而"该不该提交"主要靠这两样。
    `required_edges` 的静态校验只认直接上游（core/dag.py:234），所以 fix / test 两条
    直达边**必须显式写**，不是冗余。
    """
    node = load().dag.nodes["approve-commit"]
    assert node.join == "all"
    assert set(node.required_edges) == {"fix", "test", "review"}
    assert {e.source for e in node.in_edges} == {"fix", "test", "review"}


def test_both_gates_are_continue_and_have_reject_edges() -> None:
    """两道门都是 `continue`，且**各自**都有 `approved == false → recap` 边。

    `continue` 的语义就是"沿图上那条驳回边路由"——边不存在的话，驳回后下游全部失活、
    run 照样 done 却什么都不记。而**加载期校验只拦反方向**（`abort` + 驳回边 = 矛盾，
    `_check_on_reject_consistency` 直接报 `WorkflowDAGError`）；`continue` 而没有边
    **是合法的**、静默通过。所以这条只能靠测试守住。

    判据看**状态**不看**动作**：`rejected_abort_node()` 按节点状态判定，因为 queue 模式下
    Worker 经 `from_checkpoint` 重建后直接 `run()`，**不经过 `approve()`**。
    """
    dag = load().dag
    for nid in ("approve-plan", "approve-commit"):
        assert dag.nodes[nid].on_reject == "continue", nid
        reject_edges = [e for e in dag.nodes["recap"].in_edges if e.source == nid]
        assert reject_edges, f"{nid} 声明 continue 却没有驳回边"
        assert reject_edges[0].when == f"$.nodes.{nid}.output.approved == false"


def test_both_halt_trigger_edges_present() -> None:
    """两个中断触发点各一条 when 边（不靠给下游逐条 gate）。"""
    dag = load().dag
    triggers = {e.source: e.when for e in dag.nodes["halt"].in_edges}
    assert triggers == {
        "rca": "$.nodes.rca.output.insufficient == true",
        "locate": "$.nodes.locate.output.found == false",
    }


def test_commit_is_scheduled_after_the_evidence_gate_only() -> None:
    """commit 只有一条入边，且带 `approved == true`——不可逆动作前必须有人签字。"""
    edges = load().dag.nodes["commit"].in_edges
    assert len(edges) == 1
    assert edges[0].source == "approve-commit"
    assert edges[0].when == "$.nodes.approve-commit.output.approved == true"


# ── 行为：批准链 ────────────────────────────────────────────────────────────


async def test_full_approve_chain_reaches_commit_and_recap() -> None:
    """两道门都通过 → fix/test/review → commit → recap。"""
    ex = build()
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == ["approve-plan"]

    await ex.approve("approve-plan", approved=True, by="lead-engineer")
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == ["approve-commit"]
    assert [ex.get_status(n) for n in ("fix", "test", "review")] == [DONE, DONE, DONE]
    assert ex.get_status("commit") == "pending"  # 证据门未批，还没提交

    await ex.approve("approve-commit", approved=True, by="lead-engineer")
    assert await ex.run() == "done"
    assert ex.get_status("commit") == DONE
    assert ex.get_status("recap") == DONE
    assert ex.get_status("halt") == SKIPPED


async def test_plan_gate_reject_routes_to_recap_and_skips_whole_fix_stage() -> None:
    """计划门驳回 → 走 recap、run 不中止，**整条修复段一个节点都不许跑**。

    这是 `on_reject: continue` 的语义：驳回是**人的决定**，不是执行失败——报成 run
    `failed` 会与"节点跑挂了"混为一谈（`dag_executor.py:722` 的注释也在强调这个区分）。
    故 run 判 done，留痕在 recap 与审批记录的 approver + comment 里。

    同时锁住"宁可不修"：计划没批就不该有任何代码动作，fix/test/review/commit 全 SKIPPED。
    """
    ex = build()
    await ex.run()
    await ex.approve("approve-plan", approved=False, by="lead-engineer", comment="方案不对")
    assert await ex.run() == "done"
    assert ex.get_status("approve-plan") == REJECTED
    assert ex.get_status("recap") == DONE
    for nid in ("fix", "test", "review", "approve-commit", "commit"):
        assert ex.get_status(nid) == SKIPPED, nid


async def test_evidence_gate_reject_routes_to_recap_without_aborting() -> None:
    """证据门驳回 → recap 执行、run 不中止、commit 不执行。"""
    ex = build()
    await ex.run()
    await ex.approve("approve-plan", approved=True, by="lead-engineer")
    await ex.run()
    await ex.approve("approve-commit", approved=False, by="lead-engineer", comment="测试不够")
    assert await ex.run() == "done"
    assert ex.get_status("recap") == DONE
    assert ex.get_status("commit") == SKIPPED


async def test_failed_tests_fail_the_node_and_the_run() -> None:
    """测试不过 → **test 节点判 failed、整条 run 判 failed**（不再"跳过下游、recap 收口"）。

    ## 改于 2026-09-21（原设计）

    原先 `test.passed == false` 走条件边跳过 review/证据门，由 recap 收口 ——
    也就是"跑完了但结论是不通过"在图上**仍是 done（绿）**、run 仍算 `completed`。

    **实测代价**（run_668981c0a7）：tester 输出 `{passed: false, tests_run: 0,
    failed: ["沙箱未接线，一条测试都没跑", …]}`，而节点全绿、看板显示"已完成" ——
    实际修复没落盘、一条测试都没跑。图上一片绿是最误导的一种失败。

    现在由 executor 的 `VERDICT_FIELDS` 认这个结论字段（`tester` → `passed`），
    显式 false 即节点失败。`reviewer` 的 `approved` 同理。

    ⚠️ 这**不是** halt：`halt` 是"证据不足、不往下走"（图上的 `kind`），
    这里是"结论不通过"（节点失败）。两条判据仍要分开，`halt_triggered()` 应为 False。
    """
    bad = dict(OK_OUTPUTS, test={"passed": False, "tests_run": 3, "failed": ["T1"]})
    ex = build(bad)
    await ex.run()
    await ex.approve("approve-plan", approved=True, by="lead-engineer")
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.get_status("test") == FAILED
    assert ex.halt_triggered() is False


async def test_review_reject_fails_the_node_and_the_run() -> None:
    """审查不通过 → **review 节点判 failed、run 判 failed**（同 test，见上一条的说明）。

    改动原因与 `test_failed_tests_fail_the_node_and_the_run` 同：`approved: false`
    以前只是"跳过证据门"，节点仍是绿的。
    """
    bad = dict(OK_OUTPUTS, review={"approved": False, "comments": ["回归风险"], "risk": "high"})
    ex = build(bad)
    await ex.run()
    await ex.approve("approve-plan", approved=True, by="lead-engineer")
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.get_status("review") == FAILED
    assert ex.halt_triggered() is False


# ── 行为：中断（halt）────────────────────────────────────────────────────────


async def test_locate_not_found_halts_before_fix() -> None:
    """`locate.found == false` → halt，**修复链一个节点都不许跑**。

    这是 docs/TODO.md §20 那个坑的回归：locate 报负证据后若无人拦，`fix` 会从 plan 的
    文字里猜一个仓库去改，而工作区默认全量准备、service 传错也不报错 → 猜错静默通过。
    """
    bad = dict(OK_OUTPUTS, locate={"found": False, "summary": "未匹配到仓库", "missing": ["repo"]})
    ex = build(bad)
    assert await ex.run() == "done"
    assert ex.get_status("halt") == DONE
    assert ex.halt_triggered() is True
    for nid in ("rca", "plan", "approve-plan", "fix", "test", "review", "commit"):
        assert ex.get_status(nid) == SKIPPED, nid


async def test_rca_insufficient_halts_before_plan() -> None:
    """根因环节判定证据不足 → halt，不再出计划（否则 plan 会照着编出来的根因写方案）。"""
    bad = dict(OK_OUTPUTS, rca={
        "insufficient": True, "confidence": 0.1, "summary": "证据不足以定根因",
        "hypotheses": [], "ruled_out": [], "missing": ["logs"],
    })
    ex = build(bad)
    assert await ex.run() == "done"
    assert ex.get_status("rca") == DONE
    assert ex.get_status("halt") == DONE
    assert ex.get_status("plan") == SKIPPED


# ── 行为：入参预检 ──────────────────────────────────────────────────────────


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
