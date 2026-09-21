"""Problem Center「分析new」workflow（scripts/problem-log-diagnose.workflow.yaml）。

锁五件事：
1. **能加载**——即已过 `core/dag.py` 的静态校验（无环 / join 一致性 / params 引用只指上游）；
2. **拓扑合理**——日志证据先于根因、门等齐 rca + plan、两条中断边存在；
3. **门是终态节点**——图上**没有出边**，`on_reject: continue`；通过/驳回都是 run 的自然收敛
   （run 仍判 `done`，人工决策不是执行失败）；
4. **halt 的裁决权在 executor**——触发后其余 PENDING 节点全部 SKIPPED，不靠逐条 gate 边；
5. **会触发 run 工作区准备**——否则 `code-locator` 的 `ws_*` 全 fail-closed，诊断链直接断
   （见 `test_workspace_gets_prepared_for_the_diagnosis_chain`，那是实际踩过的静默回归）。

⚠️ 历史（2026-09-21，修复段整体删除）：
本流程原本是 `plan → approve-plan → fix → test → review → approve-commit → commit → recap`，
两道人工门、产出是沙箱内改好的代码 + 一个本地 commit。删掉的理由：Problem Center 里人要拍板的
是「这条问题单要不要处理、派给谁做」，不是在沙箱里替人改代码。现在三种裁定
（拒绝重跑 / 忽略关单 / 升级开单）都收在同一道门上，**「升级」的下游动作（建工单、绑号、
置终态）住在 APM 侧，不在本图里**——所以门后面没有节点，不是漏写。

⚠️ 历史（两次修正，方向相反，值得留）：

本文件原有一条 `test_only_reject_edge_reaches_recap`，断言"引擎忽略 `on_reject`、拒绝路由完全
依赖那条 when 边"。**引擎补上消费方之后那条断言就错了**——`on_reject: abort` 真会中止 run
（`dag_executor.py:321 rejected_abort_node()` + `:723`），那时 `approve-plan → recap` 边
**永远不可达**（CLAUDE.md §4.1：「图在骗人，但加载与运行都不报错」），而测试一直在锁这个错行为。

**修法二选一，两边都自洽**：改 `abort` + 删边，或改 `continue` + 留边。本项目选的是 `continue`
——理由是"驳回"是**人的决定**，把它报成 run `failed` 会与"执行出错"混为一谈
（`dag_executor.py:722` 的注释也在强调这个区分）。修复段删除后那条 `→ recap` 边一并没了，
于是本图落在**第三种形态**：`continue` **且没有驳回出边**。这一形态是引擎明确允许的
（`_check_on_reject_consistency` 只拦反方向「abort + 有驳回边」那个矛盾组合；
`tests/test_run_api.py` 的 `APPROVAL_YAML` 是同形状的先例），但**加载期不查**，只能靠本文件守住。

⚠️ 与上游的两条用例合并（2026-09-21，rebase `7c44b21`）：
上游同一天加了 `test_failed_tests_fail_the_node_and_the_run` 与
`test_review_reject_fails_the_node_and_the_run`（`f2c7bb0`「跑完了但结论是不通过」判节点失败），
它们用的正是本流程图里的 `test` / `review` 节点——而本次重构把修复段整段删了，
**那两个节点在这条流程里已不存在**，两条用例随之失去载体。

**不是静默删掉**：那两条测的是 executor 层的 `VERDICT_FIELDS` 机制，而
`tests/test_executor.py` 有它的通用覆盖（5 条）；其中 `reviewer → approved` 那半边原先
**只有**这两条 workflow 级用例在测，删除时会成为唯一的覆盖缺口，因此已改写成
`test_executor.py` 的 `test_every_verdict_field_is_enforced`——**对着 `VERDICT_FIELDS`
映射表参数化**，以后往表里加 agent 会自动要求覆盖。
> 场景流程（`agentflow/seed/workflows/` 的 scenario1/scenario2）**仍有** tester/reviewer，
> 但它们在 workflow 级没有对应用例（上游也没有）——要用例的话该单独加在那两条上。

⚠️ 已知的语义代价（别当它是 bug）：门是终态节点后，**通过与否只看节点状态**——
run 都是 `done` → API `success` → viewmodel `completed`。人否了诊断，run 仍报成功，
痕迹只在门节点的 `rejected` 状态与审批记录的 approver/comment 里。
这是 `continue` 语义的固有属性（`core/dag.py` 的 `TERMINAL` 含 `REJECTED`），换 `abort`
就会把它变成"执行失败"，取舍见上。

用脚本化 runner（不调 LLM、不连 MCP），跑得快且确定性。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow.core.dag import DONE, REJECTED, SKIPPED
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import DAGExecutor, WorkflowNodeFailed
from agentflow.statestore.memory import InMemoryStateStore

WF_PATH = Path(__file__).resolve().parent.parent / "scripts" / "problem-log-diagnose.workflow.yaml"

GATE = "diagnose-output"

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
#: 默认 mock runner 返回 `{"node": ..., "ok": True}`，任何 `when: $.nodes.X.output.found == false`
#: 之类的条件边都会因取不到字段而不满足（`$.nodes.locate.output.found` 恒 None ≠ false），
#: 于是本该中断的路径静默走成正常路径（第一次跑就踩到了）。
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
    # `fix/test/review/approve-commit/commit/recap` 已移除：本流程不再改代码（见文件头）。
    assert set(dag.nodes) == {"triage", "logs", "locate", "rca", "plan", GATE, "halt"}
    assert dag.nodes[GATE].is_approval
    assert dag.nodes["halt"].is_halt


def test_gate_is_terminal_with_no_out_edges() -> None:
    """门是**终态节点**：图上一条出边都没有。

    这不是漏写，是设计的落点——本流程的产物是「诊断输出 + 人的裁定」，裁定之后
    要么由 APM 起新一轮 run（拒绝）、要么由 APM 建工单（升级），**没有本图内的下游**。

    ⚠️ `Node` 只有 `in_edges`/`upstreams`，**没有 `out_edges`**，所以要从 `dag.edges` 反查。
    这条断言真正的价值在**防止误加边**：为了"看起来完整"补一条
    `approved == false → halt` 之类，会把"人否决"错当成"中断"——halt 一旦执行会把其余
    PENDING 节点全部 SKIPPED（CLAUDE.md §3.1），语义完全不同。
    """
    dag = load().dag
    assert [e for e in dag.edges if e.source == GATE] == []


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


def test_gate_waits_for_rca_and_plan() -> None:
    """门必须等齐 rca 与 plan，且两条都是**直接**上游。

    审批卡片只画 `node.upstreams`（api/app.py 的 pending_approvals），缺一条审批人就看不到
    根因或方案。默认 `join: any` 会让 plan 一完成就弹门——那时 rca 还没进 upstream。
    """
    node = load().dag.nodes[GATE]
    assert node.join == "all"
    assert set(node.required_edges) == {"rca", "plan"}
    assert {e.source for e in node.in_edges} == {"rca", "plan"}


def test_gate_is_continue_so_reject_does_not_abort() -> None:
    """门必须显式写 `on_reject: continue`——默认值是 `abort`，那会把"人否决"报成 run 失败。

    ⚠️ 本图里 `continue` 是**没有配套驳回边**的（门是终态节点，见上）。这一组合
    **加载期不查**：`_check_on_reject_consistency` 只拦反方向「`abort` + 有驳回边」那个矛盾组合
    （`core/dag.py`），所以"图里到底写没写 `continue`"只能靠这条断言守住。
    """
    assert load().dag.nodes[GATE].on_reject == "continue"


def test_both_halt_trigger_edges_present() -> None:
    """两个中断触发点各一条 when 边（不靠给下游逐条 gate）。"""
    dag = load().dag
    triggers = {e.source: e.when for e in dag.nodes["halt"].in_edges}
    assert triggers == {
        "rca": "$.nodes.rca.output.insufficient == true",
        "locate": "$.nodes.locate.output.found == false",
    }


# ── 行为：通过 / 驳回 ───────────────────────────────────────────────────────


async def test_approve_converges_to_done_and_does_not_re_park() -> None:
    """通过 → run 收敛到 `done`（**不是再次 `waiting_approval`**），门 DONE，全程无中断。

    「不再二次停放」这条是**并发配额泄漏的回归**：门若是终态节点而引擎仍把它算作可等待，
    run 会永远停在活动状态集里，每跑一次漏一个租户额度。正常路径下唯一的 SKIPPED 是 `halt`
    （两条触发边都不满足）——**"有节点被跳过"不等于"中断"**，这正是 `halt_triggered()`
    按图上的 `kind` 判定、而不是数 SKIPPED 的原因（CLAUDE.md §3.1）。
    """
    ex = build()
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == [GATE]
    assert ex.get_status("halt") == SKIPPED  # 触发边不满足 → 自动跳过，非中断

    await ex.approve(GATE, approved=True, by="lead-engineer")
    assert await ex.run() == "done"
    assert ex.get_status(GATE) == DONE
    assert ex.pending_approvals() == []
    assert ex.halt_triggered() is False
    assert ex.rejected_abort_node() is None
    for nid in ("triage", "logs", "locate", "rca", "plan"):
        assert ex.get_status(nid) == DONE, nid


async def test_reject_does_not_abort_run_and_leaves_no_pending_gate() -> None:
    """驳回 → run 仍判 `done`（**不是 `failed`**），门留 `rejected`，没有待办的门。

    这是 `on_reject: continue` 的语义：驳回是**人的决定**，不是执行失败——报成 run
    `failed` 会与"节点跑挂了"混为一谈。留痕在门节点的 `rejected` 状态与审批记录的
    approver + comment 里；「重新分析」由 APM 另起一轮 run 实现，不在本图内。

    `pending_approvals() == []` 是要点：门被答复后必须从待办里消失，否则调用方
    （APM 的决策端点）会以为门上还挂着人，重复下发裁定。
    """
    ex = build()
    await ex.run()
    await ex.approve(GATE, approved=False, by="lead-engineer", comment="方案不对")
    assert await ex.run() == "done"
    assert ex.get_status(GATE) == REJECTED
    assert ex.pending_approvals() == []
    assert ex.rejected_abort_node() is None  # continue → 不中止
    assert ex.halt_triggered() is False


# ── 行为：中断（halt）────────────────────────────────────────────────────────


async def test_locate_not_found_halts_before_plan_and_gate() -> None:
    """`locate.found == false` → halt，**诊断链下游一个节点都不许跑**。

    `found: false` 意味着代码证据是空的：该中断而不是让人对着半份证据拍板
    （docs/TODO.md §20 那个坑的回归——当时无人拦，`fix` 会从 plan 的文字里猜一个仓库去改，
    而工作区默认全量准备、service 传错也不报错 → 猜错静默通过）。
    修复段删掉后这条 gate 仍然要留，保住的是「没有代码证据就不出方案、不开单」。

    `pending_approvals() == []` 是新增要点：halt 后 `_process_skips` 把其余 PENDING
    一律 SKIPPED（**不区分审批节点**），所以**门不一定存在**——调用方不能假设有门可答。
    """
    bad = dict(OK_OUTPUTS, locate={"found": False, "summary": "未匹配到仓库", "missing": ["repo"]})
    ex = build(bad)
    assert await ex.run() == "done"
    assert ex.get_status("halt") == DONE
    assert ex.halt_triggered() is True
    assert ex.pending_approvals() == []
    for nid in ("rca", "plan", GATE):
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
    assert ex.halt_triggered() is True
    assert ex.get_status("plan") == SKIPPED
    assert ex.get_status(GATE) == SKIPPED


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


def test_workspace_gets_prepared_for_the_diagnosis_chain() -> None:
    """本流程必须触发 run 工作区准备——否则 `code-locator` 的 `ws_*` 全部 fail-closed。

    ⚠️ 这是**实际踩过的静默回归**（2026-09-21）：删掉修复段后，图里会碰工作区的 agent
    就只剩 `code-locator` 一个，而它当时不在 `WORKSPACE_AGENTS` 里 →
    `RunService._prepare_workspace` 提前 return → 工作区不存在 →
    `ws_read_file` / `ws_list_files` 报「在本次 run 未 prepare」→ `code-locator` 退到
    MCP 上逐个猜项目名（实测烧掉 16 万 token、耗尽了迭代预算、输出非 JSON）→
    `locate.found == false` → **整条诊断链在 halt 处中断**。

    症状是「诊断分析不出问题」，离根因（一个跟诊断无关的名单）很远，故在这里锁住：
    **判据是"这个 agent 用不用工作区"，不是"它写不写代码"。**
    """
    from agentflow.service import WORKSPACE_AGENTS

    agents = {n.agent for n in load().dag.nodes.values()}
    assert agents & WORKSPACE_AGENTS, (
        f"本流程的 agent {sorted(a for a in agents if a)} 没有任何一个会触发工作区准备；"
        f"`code-locator` 的 ws_read_file/ws_list_files 会全部 fail-closed（WORKSPACE_AGENTS={sorted(WORKSPACE_AGENTS)}）"
    )
