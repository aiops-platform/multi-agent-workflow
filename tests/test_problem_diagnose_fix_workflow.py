"""Problem Center 修复段 workflow（`agentflow/seed/workflows/problem-diagnose-fix.yaml`）。

由 `bug-fix-scenario2` 简化而来：诊断链整段去掉，诊断结论走**入参** `rca`。
本文件锁五件事——它们的共同点是**坏了不会报错**：

1. **能加载**——即已过 `core/dag.py` 的静态校验（无环 / 悬空 / join 一致性 /
   params 只引上游）。种子侧还有一道 CI 闸门（`tests/test_seed_defaults.py` 的
   corpus + schema 字段核对），两边都要过。
2. **诊断是入参、不是节点**——`plan.require` 含 `rca`。少一个 `require`，缺诊断时
   params 静默解析成 None，fix-planner 转头对着工单标题**编一份计划**，整条 run 照样绿。
3. **服务名只有一个来源**——`fix` / `remediate` / `test` / `commit` 都 `require: [service]`
   且取自工单的 CMDB CI。没有 locate 节点 ⇒ 缺了必须失败，不能让 agent 猜一个仓库去改
   （`docs/TODO.md` §20，实测 run_c9eb2fe68d）。
4. **两道门的语义相反、且都显式写出来**——`approve-plan` 是 `abort`（驳回 = 中止，
   且**没有**驳回边）；`approve-commit` 是 `continue`（驳回 → recap，不中止）。
   `on_reject` 默认值是 `abort`，写错方向图上不会报错，只会静默换语义。
5. **`ticket-done` 只在成功路径上**——它挂在 `commit` 之后。位置错了的后果不是报错，
   而是"测试没过 / 审批驳回"那几条路**也不再回传工单**（或反过来重复回传）。

用脚本化 runner（不调 LLM、不连 MCP），跑得快且确定性。
"""
from __future__ import annotations

import pytest
import yaml

from agentflow.core.dag import DONE, REJECTED, SKIPPED
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import DAGExecutor, WorkflowNodeFailed
from agentflow.seed import load_workflow_seeds
from agentflow.statestore.memory import InMemoryStateStore

#: 从**种子**里取 YAML 而不是从文件路径取：这条流程是靠种子发给新租户的，
#: 走的正是 `load_workflow_seeds()`（manifest → 文本 → 逐字节入库）。
#: 直接读文件的写法会在"manifest 忘了登记"时照样通过——那正是要拦的。
SEED_ID = "seed-problem-diagnose-fix"

PLAN_GATE = "approve-plan"
COMMIT_GATE = "approve-commit"

INPUTS = {
    "bug_report": {
        "number": "PR-0007",
        "short_description": "订单服务结账无响应",
        "cmdb_ci": {"name": "order-service", "namespace": "order"},
        # 诊断结论住在工单里 —— 升级建单时由上游 problem-log-diagnose 的 rca / plan
        # 两个节点写进来（`ticket_store._ticket_fields_from_params`）。**不是**顶层
        # `inputs.rca`：那条路径没有任何调用方会填（踩过：升级建的单点「发起」必挂）。
        "diagnosis": {
            # RootCauseSchema 形态
            "rca": {
                "insufficient": False,
                "confidence": 0.82,
                "root_cause_type": "code_bug",
                "summary": "template 为 null 时未校验",
                "hypotheses": ["fin 参数缺失"],
                "ruled_out": ["基础设施"],
            },
            # 诊断侧 plan（remediation-planning-analyst）的产出形态
            "plan": {"summary": "补空值校验", "steps": [{"type": "code_fix", "target": "QuotationService.java"}]},
        },
    },
}

#: 各节点的"正常路径"输出（键名与各 agent 的 schema 对齐）。**必须给全**：
#: 默认 mock 输出 `{"node": ..., "ok": True}` 里没有 `passed` / `approved` / `delivered`，
#: 而 `when: $.nodes.test.output.passed == true` 之类的边取不到字段即不满足 ——
#: 条件边会静默走成"不通过"，测试于是测的是一条别的路径。
OK_OUTPUTS: dict[str, dict] = {
    "plan": {"summary": "补空值校验", "steps": [{"type": "code_fix", "target": "QuotationService.java"}]},
    "fix": {"diff": "--- a/QuotationService.java\n+++ b/QuotationService.java\n", "files_changed": ["QuotationService.java"], "explanation": "加空值校验"},
    "remediate": {"changes": [{"action": "restart_pod", "namespace": "order", "params": {}}]},
    "test": {"passed": True, "tests_run": 12, "failed": []},
    "review": {"approved": True, "comments": [], "risk": "low"},
    "commit": {"pr_url": "https://github.com/acme/order-service/pull/42", "pr_number": 42, "base_sha": "abc123"},
    "ticket-done": {"payload": {"ticket_id": "PR-0007", "status": "resolved", "description": "已修复"}, "delivered": True, "note": ""},
    "recap": {"summary": "已闭环", "root_cause": "空值未校验", "actions": ["修复并提交 PR"], "followups": []},
}


def load() -> Workflow:
    yaml_text = next(w["yaml"] for w in load_workflow_seeds() if w["id"] == SEED_ID)
    return Workflow.load_yaml(yaml_text)


def build(
    outputs: dict | None = None, inputs: dict | None = None, seen: dict | None = None
) -> DAGExecutor:
    """按节点 id 返回脚本化输出；未给出的节点回落到 mock 形态。

    ``seen``：给了就把每个节点**解析后**的 params 记进去（键=节点 id）。断言"下游到底
    收到了什么"只能靠它 —— ``node_states[nid]["output"]`` 是 agent 的**输出**，不是入参。
    """
    table = OK_OUTPUTS if outputs is None else outputs

    async def runner(node, params):
        if seen is not None:
            seen[node.id] = params
        return table.get(node.id, {"node": node.id, "ok": True})

    wf = load()
    return DAGExecutor(
        "run_pdf", "otr", wf.dag, InMemoryStateStore(),
        inputs=inputs if inputs is not None else dict(INPUTS), node_runner=runner,
    )


# ── 结构守卫 ────────────────────────────────────────────────────────────────


def test_workflow_loads_with_exactly_the_expected_nodes() -> None:
    """节点集合就是这条流程的定义本身——多一个少一个都是语义变化。

    ⚠️ 断言**集合相等**而不是包含：漏掉 `remediate`（止血）或 `ticket-done`（工单闭环）
    都是"图上少一块"，跑起来不会有任何报错。
    """
    dag = load().dag
    assert set(dag.nodes) == {
        "plan", PLAN_GATE, "fix", "remediate", "test", "review",
        COMMIT_GATE, "commit", "ticket-done", "recap",
    }
    assert dag.nodes[PLAN_GATE].is_approval
    assert dag.nodes[COMMIT_GATE].is_approval
    # 诊断段整段不在图里——它们是被简化掉的部分
    assert not {"triage", "scope", "logs", "trace", "metrics", "infra", "locate", "know", "rca", "halt"} & set(dag.nodes)


def test_seed_has_no_header_comments_inside_params_keys() -> None:
    """YAML 正文逐字节入库、并显示在 UI 的 YAML 编辑框里（seed/README）。

    这里不校验风格，只钉一件事实：**它能被 yaml.safe_load 解析**且 name 与文件名对得上
    （manifest 的 file 写错时，播种器只 warn + 跳过 → 新租户静默少一条流程）。
    """
    raw = yaml.safe_load(next(w["yaml"] for w in load_workflow_seeds() if w["id"] == SEED_ID))
    assert raw["name"] == "problem-diagnose-fix"


def test_plan_takes_the_diagnosis_from_the_ticket_and_fails_fast_without_it() -> None:
    """`plan` 的三个入参都取自**工单里那份诊断**，且 `require` 把它们设成硬前置。

    没有 `require` 时，缺键只会让 params 解析成 None —— fix-planner 收到一个空输入却不报错，
    会照着工单标题编一份计划，run 一路绿到底。

    ⚠️ 路径必须与**建单节点写诊断的位置**一致（`inputs.bug_report.diagnosis.*`，
    见 `ticket_store._ticket_fields_from_params`）。曾经写成顶层的 `$.inputs.rca`——
    而没有任何调用方往顶层放 rca，于是升级建出来的单点「发起」**必然在 plan 处 fail-fast**。
    """
    node = load().dag.nodes["plan"]
    assert node.agent == "fix-planner"  # 不是诊断侧的 remediation-planning-analyst（那个出的是方案对比）
    assert node.params["problem"] == "$.inputs.bug_report"
    assert node.params["rca"] == "$.inputs.bug_report.diagnosis.rca"
    assert node.params["solution"] == "$.inputs.bug_report.diagnosis.plan"
    assert set(node.require) == {"problem", "rca", "solution"}
    assert node.on_failure == "abort"


@pytest.mark.parametrize("nid", ["fix", "remediate", "test", "commit"])
def test_every_workspace_node_requires_the_service_from_the_ticket(nid: str) -> None:
    """凡是要动工作区/线上资源的节点，`service` 都必须来自工单且**必须存在**。

    这是 `docs/TODO.md` §20 那个坑的锁：`ws_*` 工具只校验"这个 service 备过工作区没有"，
    而工作区**默认全量准备** —— service 传错/传空不会报"服务不存在"，只会改错仓库。
    本图没有 locate 节点，工单的 CMDB CI 是唯一的服务来源。
    """
    node = load().dag.nodes[nid]
    assert "service" in node.require, f"{nid} 没有 require service —— 空服务名会静默走到猜仓库那条路"
    assert node.params["service"] == "$.inputs.bug_report.cmdb_ci.name"


def test_the_repair_chain_is_linear() -> None:
    """`fix → remediate → test → review`（本图唯一的顺序性断言）。

    全线性 ⇒ 每个节点只有一条入边 ⇒ 不需要 `join: all` / `required_edges`。
    改成并行（止血与修代码同时跑）的话，`test` 会两条入边、必须显式 `join: all`，
    否则它会**在 fix 或 remediate 还没跑完时**被调度，params 解析成 None。
    """
    dag = load().dag
    assert [(e.source, e.target) for e in dag.edges if e.source in {"fix", "remediate", "test"}] == [
        ("fix", "remediate"),
        ("remediate", "test"),
        ("test", "review"),  # when 条件边，见下一条
    ]
    for nid in ("fix", "remediate", "test"):
        assert len(dag.nodes[nid].in_edges) == 1, f"{nid} 不止一条入边——join 语义需要显式声明"


def test_test_and_review_verdicts_are_failures_not_edges() -> None:
    """`passed == false` / `approved == false` **没有出边**——它们由 executor 判节点失败。

    那两条"→ recap"的边在 `VERDICT_FIELDS` 落地后就**永远不可达**了（失败节点的出边恒失活）。
    留着的代价不是报错，是读图的人以为"测试没过还能走到复盘收口"。
    代价如实记在这里：测试/审查不通过时 run 判 failed、没有复盘。
    """
    dag = load().dag
    assert [(e.target, e.when) for e in dag.edges if e.source == "test"] == [
        ("review", "$.nodes.test.output.passed == true")
    ]
    assert [(e.target, e.when) for e in dag.edges if e.source == "review"] == [
        (COMMIT_GATE, "$.nodes.review.output.approved == true")
    ]


def test_plan_gate_aborts_and_has_no_reject_edge() -> None:
    """计划门：`abort` + **没有**驳回边（这一组合是刻意的，加载期只查反方向）。

    `abort` 下驳回 = 中止整条 run；此时若还写了 `approved == false → recap`，
    那条边永远不可达——图在骗人，而加载与运行都不报错（`CLAUDE.md` §4.1）。
    """
    dag = load().dag
    assert dag.nodes[PLAN_GATE].on_reject == "abort"
    assert [(e.target, e.when) for e in dag.edges if e.source == PLAN_GATE] == [
        ("fix", f"$.nodes.{PLAN_GATE}.output.approved == true")
    ]


def test_commit_gate_continues_and_routes_reject_to_recap() -> None:
    """提交门：`continue` + 两条出边（放行 → commit；驳回 → recap）。

    必须显式写 `continue`：默认是 `abort`，那会把**人的否决**报成 run `failed`
    （"执行出错"的语义）——而这里驳回是设计内的收敛路径。
    """
    dag = load().dag
    assert dag.nodes[COMMIT_GATE].on_reject == "continue"
    assert sorted((e.target, e.when) for e in dag.edges if e.source == COMMIT_GATE) == sorted([
        ("commit", f"$.nodes.{COMMIT_GATE}.output.approved == true"),
        ("recap", f"$.nodes.{COMMIT_GATE}.output.approved == false"),
    ])


def test_ticket_done_sits_on_the_success_path_only() -> None:
    """工单回传挂在 `commit → ticket-done → recap` 上：**只有真的提交了才回传**。

    位置是这条节点唯一容易写错的地方（挪到 recap 前面就变成"连驳回也回传"，
    而回传是对外承诺 —— `delivered` 由 `VERDICT_FIELDS` 判红兜一层）。
    """
    dag = load().dag
    assert [e.source for e in dag.nodes["ticket-done"].in_edges] == ["commit"]
    assert [e.target for e in dag.edges if e.source == "ticket-done"] == ["recap"]
    assert dag.nodes["ticket-done"].on_failure == "abort"


def test_workspace_gets_prepared_for_the_repair_chain() -> None:
    """本流程必须触发 run 工作区准备——否则 `ws_*` 全部 fail-closed。

    判据是「图里有没有会碰工作区的 agent」（`WORKSPACE_AGENTS`），不是"有没有修复段"。
    实测踩过：名单少了 `code-locator`，诊断链在 `locate` 处整段断掉，而症状离根因很远
    （见 `docs/design-v5.8.md` §4.14 第 3 条）。
    """
    from agentflow.service import WORKSPACE_AGENTS

    agents = {n.agent for n in load().dag.nodes.values()}
    assert agents & WORKSPACE_AGENTS, f"没有任何节点会触发工作区准备（WORKSPACE_AGENTS={sorted(WORKSPACE_AGENTS)}）"


# ── 行为：两道门 ────────────────────────────────────────────────────────────


async def test_happy_path_parks_at_both_gates_and_converges() -> None:
    """计划门 → 提交门 → 提交 → 回传 → 复盘，全程无中断。

    「两次停放」是这条流程的形状：计划门在动手**之前**（审方案），提交门在推 PR **之前**
    （审证据）。任一处不批，下游都不该跑。
    """
    seen: dict = {}
    ex = build(seen=seen)
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == [PLAN_GATE]
    assert ex.get_status("fix") == "pending"  # 计划没批之前，一行代码都不许改

    await ex.approve(PLAN_GATE, approved=True, by="lead-engineer")
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == [COMMIT_GATE]
    # 提交门之前，验证链已经跑完了（人看的是 diff + 测试证据，不是承诺）
    for nid in ("fix", "remediate", "test", "review"):
        assert ex.get_status(nid) == DONE, nid
    assert ex.get_status("commit") == "pending"  # 没批就不推 PR

    await ex.approve(COMMIT_GATE, approved=True, by="lead-engineer")
    assert await ex.run() == "done"
    assert ex.get_status("commit") == DONE
    assert ex.get_status("ticket-done") == DONE
    assert ex.get_status("recap") == DONE
    assert ex.halt_triggered() is False
    # 复盘**真的收到了**提交结果（`$.nodes.commit.output` 解析出了值，不是 None）——
    # 这条是 `$.nodes.commit.status` 那个恒空入参的同类锁（tests/test_seed_defaults.py
    # 的 test_seed_params_reference_real_schema_fields 只能核字段名，核不了"有没有值"）。
    assert seen["recap"]["commit"]["pr_url"].endswith("/pull/42")


async def test_plan_gate_reject_aborts_the_whole_run() -> None:
    """驳回计划 = 这次修复不做 → run 判 `failed`，下游一个节点都不跑。

    `approve-plan` 是 `on_reject: abort`（`rejected_abort_node()` 按**状态**判定，
    queue 模式下 Worker 不经过 `approve()` 也同构）。
    """
    ex = build()
    await ex.run()
    await ex.approve(PLAN_GATE, approved=False, by="lead-engineer", comment="方向不对")
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "approve-plan" in str(ei.value)
    assert ex.get_status(PLAN_GATE) == REJECTED
    assert ex.rejected_abort_node() == PLAN_GATE
    for nid in ("fix", "remediate", "test", "review", COMMIT_GATE, "commit", "ticket-done"):
        assert ex.get_status(nid) == SKIPPED, nid


async def test_commit_gate_reject_converges_without_committing_or_delivering() -> None:
    """驳回提交 = **不中止 run**：沿驳回边进 recap，commit / ticket-done 都不执行。

    三种后果一起断言，因为它们是同一条边决定的：run 判 `done`、不推 PR、不回传工单。
    回传特别要紧——它对原系统是"已解决"的承诺，绝不能因为"活干得差不多了"就发出去。
    """
    seen: dict = {}
    ex = build(seen=seen)
    await ex.run()
    await ex.approve(PLAN_GATE, approved=True, by="lead-engineer")
    await ex.run()
    await ex.approve(COMMIT_GATE, approved=False, by="lead-engineer", comment="风险太高")

    assert await ex.run() == "done"  # 人否决 ≠ 执行失败
    assert ex.get_status(COMMIT_GATE) == REJECTED
    assert ex.get_status("commit") == SKIPPED
    assert ex.get_status("ticket-done") == SKIPPED
    assert ex.get_status("recap") == DONE
    assert ex.rejected_abort_node() is None  # continue → 不中止
    # 复盘如实拿到"这条路径没走到提交"：commit 的输出是 None，不是某个空壳 dict。
    # 前提是 `commit` 节点**有出边被跳过**而不是压根没被引用 —— 后者会让 params 里
    # 连这个键都没有，下游分不清"没提交"与"图里忘了写"。
    assert "commit" in seen["recap"] and seen["recap"]["commit"] is None


# ── 行为：入参预检（fail-fast 而不是空转）────────────────────────────────────


async def test_missing_rca_fails_fast_at_plan() -> None:
    """没有诊断结论 → plan 的 require 拦住，不空转、不编方案。

    这条路径是**新租户的默认流程**上最容易撞到的：Ticket Inbox **手建**工单没有
    `bug_report.diagnosis`（升级建出来的单有），点「发起」就是这里失败。
    报错要能读出"缺诊断"。
    """
    bug = {k: v for k, v in INPUTS["bug_report"].items() if k != "diagnosis"}
    ex = build(inputs={"bug_report": bug})
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "rca" in str(ei.value)
    assert ex.get_status("plan") == "failed"
    assert ex.get_status(PLAN_GATE) == "pending"  # 下游没跑


async def test_missing_ticket_service_fails_fast_at_fix() -> None:
    """工单没有 CMDB CI → fix 的 require 拦住（不能让 agent 去猜一个仓库改）。

    注意它**到 fix 才失败**：plan 不需要服务名，所以计划会先出来——顺序是刻意的，
    审批人这时看到的是"计划有了、但工单缺服务名"。
    """
    inputs = {
        "bug_report": {
            "number": "PR-0008",
            "short_description": "x",
            "cmdb_ci": {"name": ""},
            # 诊断要有（否则挂在更早的 plan 上，就测不到 fix 这一关了）
            "diagnosis": INPUTS["bug_report"]["diagnosis"],
        },
    }
    ex = build(inputs=inputs)
    await ex.run()
    await ex.approve(PLAN_GATE, approved=True, by="lead-engineer")
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "service" in str(ei.value)
    assert ex.get_status("fix") == "failed"
    assert ex.get_status("remediate") == "pending"
