"""M2：并发 DAG 执行 + join/skip + 审批（design §8.2 / §8.3 / §8.6）。"""
from __future__ import annotations

import asyncio

import pytest

from agentflow.core.dag import DONE, PENDING, REJECTED, SKIPPED, WAITING_APPROVAL
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import VERDICT_FIELDS, DAGExecutor, WorkflowNodeFailed
from agentflow.statestore.memory import InMemoryStateStore

from .conftest import PARALLEL_ABORT_YAML, PARALLEL_YAML, SIMPLE_YAML


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


async def test_approval_reject_aborts_run_when_on_reject_abort() -> None:
    """`on_reject: abort` → 驳回**中止整条 run**，而不是沿拒绝边路由。

    与 test_approval_reject_routes_to_recap 是一对：同一个图、同样的驳回，
    只因 `on_reject` 取值不同而走向完全相反 —— 一个是"沿边路由到 recap"，
    一个是"整条 run 失败"。少了任一条，另一条都分不清"到底读没读这个字段"。

    背景：`on_reject` 此前是**死配置**——`core/dag.py` 解析进 Node 模型之后
    全仓没有消费方。图上写 `abort` 的节点实际一直走"沿拒绝边路由"，
    窄的那条静默胜出。
    """
    # 用 conftest 里的合法 abort 变体（abort + **无驳回边**）。
    # 不能再拿 `continue` 那张图只换字段——那会得到 abort + 驳回边的矛盾图，
    # 加载期就被 _check_on_reject_consistency 拦下。
    yaml_text = PARALLEL_ABORT_YAML
    assert "on_reject: abort" in yaml_text  # 防止 fixture 改名后本测试静默失效

    ex, _, _, _ = build_executor(yaml_text)
    await ex.run()
    await ex.approve("approve", approved=False, by="lead", comment="方案不合规")

    with pytest.raises(WorkflowNodeFailed) as exc:
        await ex.run()
    # 措辞要能区分"有人做了决定"与"节点跑挂了"
    assert "驳回" in str(exc.value)
    # 节点状态仍是 REJECTED（保留"谁驳的、理由是什么"），不是 failed
    assert ex.get_status("approve") == REJECTED
    # 中止发生在任何下游被调度之前（原断言看 recap，但 abort 变体里没有驳回路由、
    # 也就没有 recap 节点——改成看"批准侧的下游"同样成立且更贴题）
    assert ex.get_status("test") != DONE


async def test_approval_reject_aborts_on_resume_path_too() -> None:
    """驳回中止必须在**恢复路径**上同样成立——queue 模式下只有这条路。

    回归背景（实测踩过）：`on_reject` 第一版写在 `approve()` 里（驳回时 append 到
    `self.failed`），inline 模式能中止，**queue 模式不能**——Worker 是
    `from_checkpoint` 重建 executor 后直接 `run()`，**压根不调 `approve()`**
    （API 侧已 CAS 落库，恢复时该节点就是 REJECTED）。
    实测现象：驳回后 run 照样报 `done`，只有下游被 SKIPPED——**看着像"正常结束"**。

    所以判据必须是**状态**（`rejected_abort_node()`）而不是**动作**。
    本测试刻意**不调 approve()**，直接把节点置成 REJECTED 后 run —— 与 Worker 同构。
    """
    # 同 test_approval_reject_aborts_run_when_on_reject_abort：必须用无驳回边的变体
    wf = Workflow.load_yaml(PARALLEL_ABORT_YAML)
    store = InMemoryStateStore()
    runner, _ = make_runner()
    ex = DAGExecutor("run_resume_reject", "t", wf.dag, store, node_runner=runner, inputs={})

    # 模拟 Worker 恢复时的起点：上游已 done，审批已被 API 侧 CAS 置为 REJECTED
    for nid in wf.dag.nodes:
        ex.node_states[nid] = {"status": "pending", "output": None}
    ex.node_states["triage"] = {"status": DONE, "output": {"summary": "x"}}
    for nid in ("logs", "trace"):
        ex.node_states[nid] = {"status": DONE, "output": {"summary": "x"}}
    ex.node_states["rca"] = {"status": DONE, "output": {"summary": "x"}}
    ex.node_states["approve"] = {
        "status": REJECTED, "output": {"approved": False, "comment": "计划不接受"},
    }

    with pytest.raises(WorkflowNodeFailed) as exc:
        await asyncio.wait_for(ex.run(), timeout=5)
    assert "驳回" in str(exc.value)


async def test_approval_reject_does_not_abort_by_default_continue() -> None:
    """`on_reject: continue` 下驳回**不中止** run —— 与上面那条互为对照。"""
    ex, _, _, _ = build_executor(PARALLEL_YAML)
    await ex.run()
    await ex.approve("approve", approved=False, by="lead", comment="ok")
    assert await ex.run() == "done"  # 不抛


async def test_approval_cas_prevents_double_approve() -> None:

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
# 「跑完了」≠「通过了」：agent 的结论字段为 false → 节点失败（VERDICT_FIELDS）
# ──────────────────────────────────────────────────────────────────

VERDICT_YAML = """
name: verdict
version: "1.0.0"
inputs: {}
nodes:
  t:
    agent: tester
    on_failure: continue
  r: { agent: postmortem }
edges:
  - { from: t, to: r }
"""


async def test_negative_verdict_marks_node_failed_not_done() -> None:
    """tester 输出 `passed: false` → 节点 **failed**（不是 done），run 判 failed。

    实测背景（run_668981c0a7）：tester 输出 `{passed: false, tests_run: 0,
    failed: ["沙箱未接线，一条测试都没跑", …]}`，节点却是 `done`（绿）、
    run 走完算 `completed` —— 图上一片绿，实际什么都没验证。
    """
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(VERDICT_YAML)

    async def negative_runner(node, params):
        return {"passed": False, "tests_run": 0, "failed": ["沙箱未接线，一条测试都没跑"]}

    store = InMemoryStateStore()
    ex = DAGExecutor("run_v", "t", wf.dag, store, node_runner=negative_runner)
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.get_status("t") == "failed", "结论不通过的节点必须是 failed，不能是 done"


async def test_negative_verdict_ignores_on_failure_continue() -> None:
    """`on_failure: continue` **不能**把"结论不通过"变回 DONE。

    这是刻意设计：`continue` 的语义是"这条取证路失败就出负证据、下游照走"，
    用在**诊断侧**（logs/metrics/infra）。而"测试没过"是一个**结论**，
    不该因为某个节点的 on_failure 配置就被粉饰成"跑完了"。
    故判定放在 on_failure 之外（executor 层），见 VERDICT_FIELDS 的注释。
    """
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(VERDICT_YAML)   # t 显式声明了 on_failure: continue

    async def negative_runner(node, params):
        return {"passed": False, "tests_run": 0}

    ex = DAGExecutor("run_v2", "t", wf.dag, InMemoryStateStore(), node_runner=negative_runner)
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.get_status("t") == "failed"


async def test_negative_verdict_keeps_output_as_evidence() -> None:
    """失败节点要**保留输出** —— 用户要看的就是那份证据。

    正常失败（跑挂）没有输出，`output` 为 None；结论型失败必须带上，
    否则节点详情里只剩一句"结论不通过"，把"哪几条没过"给扔了。
    """
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(VERDICT_YAML)
    payload = {"passed": False, "tests_run": 0, "failed": ["用例 A 未通过", "用例 B 未通过"]}

    async def negative_runner(node, params):
        return dict(payload)

    store = InMemoryStateStore()
    ex = DAGExecutor("run_v3", "t", wf.dag, store, node_runner=negative_runner)
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.node_states["t"]["output"] == payload

    # checkpoint 里也要有（前端读的是 GET /runs/{id} → 库里的 nodes 行）
    rows = await store.get_nodes("run_v3")
    assert rows["t"]["output"] == payload


# ── ticket-done：最后一公里（2026-09-21 加）──────────────────────────
#
# 与 tester/reviewer 同构，但性质不同：那两者是"结论不通过"，它是**没交付**。
# 对下游读的人来说两者没有区别 —— 反正是没更新。

TICKET_DONE_YAML = """
name: ticket-verdict
version: "1.0.0"
inputs: {}
nodes:
  td: { agent: ticket-done }
edges: []
"""


async def test_undelivered_ticket_marks_node_failed() -> None:
    """`ticket-done` 报 `delivered: false` → 节点 **failed**，不是 done。

    实测背景（run_843dd83d86）：`commit` 产出 `pr_url: ""` / `pr_number: 0`
    （没有任何交付物），`ticket-done` 如实报
    `{"delivered": false, "payload": {"status": "failed", …}}` ——
    而整条 run 显示 `success`。**绿着一条没交付的 run 比红着更危险**：
    看板会把它算成已闭环。
    """
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(TICKET_DONE_YAML)
    note = "未投递：本节点目前不具备任何投递能力"

    async def undelivered_runner(node, params):
        return {
            "payload": {"ticket_id": "INC95528", "status": "failed", "description": "根因：…"},
            "delivered": False,
            "note": note,
        }

    store = InMemoryStateStore()
    ex = DAGExecutor("run_td", "t", wf.dag, store, node_runner=undelivered_runner)
    with pytest.raises(WorkflowNodeFailed) as excinfo:
        await ex.run()
    assert ex.get_status("td") == "failed", "没交付的节点必须是 failed，不能是 done"
    # 失败理由要带上那句诚实说明（理由链尾的 `note`），不能只剩"结论为不通过"
    assert note in str(excinfo.value)


async def test_delivered_ticket_stays_done() -> None:
    """负向对照：投递成功（`delivered: true`）**不能**被误判成失败。

    只有 `is False` 才判 —— 这是 `VERDICT_FIELDS` 的单边定义（见其注释）。
    没有这条，"加一个结论字段"就变成了"凡是这个 agent 都红"。
    """
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(TICKET_DONE_YAML)
    payload = {
        "payload": {"ticket_id": "INC95528", "status": "resolved"},
        "delivered": True,
        "note": "已 POST 回原系统，HTTP 200",
    }

    async def delivered_runner(node, params):
        return dict(payload)

    store = InMemoryStateStore()
    ex = DAGExecutor("run_td2", "t", wf.dag, store, node_runner=delivered_runner)
    assert await ex.run() == "done"
    assert ex.get_status("td") == "done"


async def test_passing_verdict_stays_done() -> None:
    """反向对照：`passed: true` 照常 done —— 判据只有"显式 false"一条。"""
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(VERDICT_YAML)

    async def ok_runner(node, params):
        return {"passed": True, "tests_run": 3}

    ex = DAGExecutor("run_v4", "t", wf.dag, InMemoryStateStore(), node_runner=ok_runner)
    assert await ex.run() == "done"
    assert ex.get_status("t") == "done"


async def test_verdict_field_absent_does_not_fail() -> None:
    """字段缺失**不判失败** —— 判据单边定义。

    "agent 没按契约输出" 与 "测试没过" 是两回事；用缺字段推断"没通过"会把
    前者误报成后者。（后者该由输出契约去管。）
    """
    from agentflow.core.workflow import Workflow

    wf = Workflow.load_yaml(VERDICT_YAML)

    async def no_field_runner(node, params):
        return {"tests_run": 3, "note": "没给 passed"}

    ex = DAGExecutor("run_v5", "t", wf.dag, InMemoryStateStore(), node_runner=no_field_runner)
    assert await ex.run() == "done"
    assert ex.get_status("t") == "done"


@pytest.mark.parametrize(("agent", "field"), sorted(VERDICT_FIELDS.items()))
async def test_every_verdict_field_is_enforced(agent: str, field: str) -> None:
    """`VERDICT_FIELDS` 的**每一条**都要真的生效——参数化在那张映射表上。

    为什么这样写（2026-09-21）：上面几条用的都是 `tester` / `passed`，而
    `reviewer` / `approved` 那半边原先**只有** `tests/test_problem_log_diagnose_workflow.py`
    里一条 workflow 级用例在测。那条用例随该 workflow 删掉修复段而失去载体
    （它连 `test` / `review` 节点都不再有了），删的时候差点把最后一份覆盖一起删掉——
    是"删之前先问这条断言还有没有别的地方在守"才发现的。

    改成对着映射表参数化之后，**以后往 `VERDICT_FIELDS` 里加 agent 会自动要求有覆盖**：
    加一条映射 = 多一个参数化用例，不必记得回来补测试。
    """
    wf = Workflow.load_yaml(
        "name: verdict-map\ninputs: {}\nnodes:\n"
        f"  n: {{ agent: {agent} }}\n"
        "edges: []\n"
    )

    async def runner(node, params):
        return {field: False}

    ex = DAGExecutor(f"run_v_{agent}", "t", wf.dag, InMemoryStateStore(), node_runner=runner)
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.get_status("n") == "failed", f"{agent}.{field}=false 必须判 failed"


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


async def test_on_failure_continue_negative_evidence_is_readable_downstream() -> None:
    """负证据的形状必须**与正常输出同构**——否则下游读到的不是"失败"而是 null。

    缺陷形态：executor 只返回 ``{"found": False, "error": ...}``，而四个取证 agent 的
    schema 都把 ``summary`` 列为 ``required``、下游 ``rca`` 读的也正是
    ``$.nodes.<id>.output.summary``。于是**「这一路取证失败了」在下游眼里变成
    「这一路什么都没说」**——负证据要能被读到，才叫负证据。
    """
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: negative-evidence-shape
version: "1.0.0"
inputs: {}
nodes:
  logs:
    agent: log-analyst
    on_failure: continue
  rca:
    agent: root-cause
    params: { logs: "$.nodes.logs.output.summary" }
edges:
  - { from: logs, to: rca }
"""
    wf = Workflow.load_yaml(yaml_text)

    async def runner(node, params):
        if node.id == "logs":
            raise RuntimeError("ES 连接超时")
        return {"node": node.id, "params": params}

    ex = DAGExecutor(
        "run_neg", "t", wf.dag, InMemoryStateStore(), node_runner=runner, inputs={}
    )
    assert await ex.run() == "done"

    ev = ex.get_output("logs")
    assert ev["found"] is False
    assert isinstance(ev["summary"], str) and ev["summary"]
    assert "ES 连接超时" in ev["summary"]
    # 负证据必须自述"不是无异常"——下游最容易误读的就是这一点
    assert "不是" in ev["summary"]

    # 关键判据：下游按图里写的方式读，拿到的是**字符串**，不是 None
    got = ex.get_output("rca")["params"]["logs"]
    assert got is not None
    assert got == ev["summary"]


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


# ======================================================================
# 暂停语义（§8.6 pause：波间检查，当前节点跑完即暂停）
# ======================================================================
async def test_executor_pause_returns_paused_then_checkpoint_resume() -> None:
    """request_pause 置位后 run() 不再调度新节点，返回 paused；从 checkpoint
    重建（新 executor）后可继续到 done。"""
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: pause-flow
nodes:
  a: { agent: triage }
  b: { agent: root-cause, params: { code: "$.nodes.a.output.summary" } }
edges:
  - { from: a, to: b }
"""
    wf = Workflow.load_yaml(yaml_text)
    store = InMemoryStateStore()
    calls: list[str] = []

    async def runner(node, params):
        calls.append(node.id)
        return {"node": node.id, "ok": True}

    ex = DAGExecutor("run_p", "t", wf.dag, store, node_runner=runner, inputs={})
    ex.request_pause()
    assert await ex.run() == "paused"  # 未调度任何节点
    assert calls == []

    # resume：新 executor 从 checkpoint 继续到 done
    ex2 = await DAGExecutor.from_checkpoint("run_p", "t", wf.dag, store, node_runner=runner)
    assert await ex2.run() == "done"
    assert ex2.get_status("a") == DONE and ex2.get_status("b") == DONE


async def test_executor_pause_mid_wave_stops_scheduling() -> None:
    """暂停请求落在波次执行中：当前节点完成后不再调度下游。"""
    from agentflow.core.workflow import Workflow

    yaml_text = """
name: pause-mid
nodes:
  a: { agent: triage }
  b: { agent: root-cause }
edges:
  - { from: a, to: b }
"""
    wf = Workflow.load_yaml(yaml_text)
    store = InMemoryStateStore()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_runner(node, params):
        if node.id == "a":
            entered.set()
            await release.wait()  # 卡在 a 执行中（模拟长 LLM 节点）
        return {"node": node.id, "ok": True}

    ex = DAGExecutor("run_pm", "t", wf.dag, store, node_runner=gated_runner, inputs={})
    task = asyncio.create_task(ex.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)  # 确定性地停在 a 内部
        ex.request_pause()
    finally:
        release.set()  # a 跑完 → 波次结束 → 暂停生效
    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome == "paused"
    assert ex.get_status("a") == DONE
    assert ex.get_status("b") == "pending"  # 未被调度


# ======================================================================
# 审批超时 → 拒绝路径的级联 skip 收敛（回归：曾误抛 WorkflowStalledError）
# ======================================================================
_CASCADE_SKIP_YAML = """
name: cascade-skip
nodes:
  a: { agent: triage }
  # on_reject 必须显式写 continue：默认是 abort，而 abort 下下面那条
  # `approved == false` 边永远不可达（且加载期就会被校验拦下）。
  appr: { kind: approval, name: "审批", on_reject: continue }
  b: { agent: tester, upstreams: [appr] }
  c: { kind: approval, name: "二级审批", upstreams: [b] }
  d: { agent: committer, upstreams: [c] }
  recap: { agent: postmortem, upstreams: [appr] }
edges:
  - { from: a, to: appr }
  - { from: appr, to: b, when: "$.nodes.appr.output.approved == true" }
  - { from: appr, to: recap, when: "$.nodes.appr.output.approved == false" }
  - { from: b, to: c }
  - { from: c, to: d, when: "$.nodes.c.output.approved == true" }
"""


async def test_approval_reject_cascades_skip_to_convergence() -> None:
    """审批被拒（含超时置 REJECTED_CANCELED）后，其后继链必须级联 SKIPPED 并收敛。

    回归背景：skip 判定在「非审批 ↔ 审批」两类节点间交错级联（b 变 SKIPPED 后 c 才
    可判定，c 变 SKIPPED 后 d 才可判定）。单趟扫描 + 单次 _process_approvals 只能推进
    一级；若 recap 已是最后一个 ready 节点，run() 会在链尾尚未判定时误判「无 ready、
    无 waiting、非全终态」→ 抛 WorkflowStalledError，run 卡在 running 永不收敛。
    """
    from agentflow.core.dag import REJECTED_CANCELED

    wf = Workflow.load_yaml(_CASCADE_SKIP_YAML)
    store = InMemoryStateStore()
    runner, _ = make_runner()
    ex = DAGExecutor("run_cascade", "t", wf.dag, store, node_runner=runner, inputs={})

    # 模拟真实 resume 起点：a 已 done，审批超时置 rejected-canceled，
    # recap 已执行完，b/c/d 从未被调度（无 checkpoint）
    for nid in wf.dag.nodes:
        ex.node_states[nid] = {"status": "pending", "output": None}
    ex.node_states["a"] = {"status": DONE, "output": {"summary": "x"}}
    ex.node_states["appr"] = {
        "status": REJECTED_CANCELED, "output": {"approved": False, "reason": "timeout"},
    }
    ex.node_states["recap"] = {"status": DONE, "output": {"summary": "recap"}}

    outcome = await asyncio.wait_for(ex.run(), timeout=5)
    assert outcome == "done", f"应级联收敛为 done，实际 {outcome}"
    assert ex.get_status("b") == SKIPPED
    assert ex.get_status("c") == SKIPPED   # 审批节点也被级联 skip
    assert ex.get_status("d") == SKIPPED
    assert ex.get_status("recap") == DONE


# ======================================================================
# halt 节点：中途判定证据不足 → 停在终点，且与「正常走完」**可区分**
#
# 为什么需要：条件边不满足时下游是 SKIPPED，而 SKIPPED 与 DONE **同属终态**
# → `all_terminal()` 为真 → run 照样 `done` → API 照样 `success`。
# 实测踩过：一条零证据的 run（定位不了、四个取证节点全负证据、rca 自述"证据完全缺失"）
# 走完全程报了 success。halt 让"中断"成为**可判定的事实**。
# ======================================================================
HALT_YAML = """
name: halt-demo
version: "1.0.0"
inputs:
  bug: { type: object, required: true }
nodes:
  scope:
    agent: scope
  probe:
    agent: probe
    params: { s: "$.nodes.scope.output.summary" }
  recap:
    agent: recap
  halt:
    kind: halt
edges:
  - { from: scope, to: probe, when: "$.nodes.scope.output.insufficient == false" }
  - { from: probe, to: recap }
  - { from: scope, to: halt, when: "$.nodes.scope.output.insufficient == true" }
"""


async def _run_halt_case(insufficient: bool):
    """跑一遍 halt-demo。scope 按入参决定是否说「我定不了位」。"""
    wf = Workflow.load_yaml(HALT_YAML)
    calls: dict = {}

    async def runner(node, params):
        calls[node.id] = calls.get(node.id, 0) + 1
        if node.agent == "scope":
            return {"summary": "无法定位到具体服务", "insufficient": insufficient,
                    "missing": ["受影响服务", "发生时间", "错误原文"],
                    "candidate_services": []}
        return {"summary": f"{node.agent}-out"}

    ex = DAGExecutor("run_h", "tenant-a", wf.dag, InMemoryStateStore(), node_runner=runner)
    outcome = await ex.run()
    return ex, outcome, calls


async def test_halt_fires_and_is_distinguishable() -> None:
    """证据不足 → 走 halt；`halt_triggered()` 为真。"""
    ex, outcome, calls = await _run_halt_case(insufficient=True)

    assert ex.get_status("scope") == DONE
    # 中间节点被 SKIPPED 级联 —— 这正是「避免后续不必要的消耗」
    assert ex.get_status("probe") == SKIPPED
    assert ex.get_status("recap") == SKIPPED
    assert ex.get_status("halt") == DONE
    assert ex.halt_triggered() is True
    # ⚠️ run 的返回值仍是 `done` —— **这是刻意的**：判"中断"用 halt_triggered()，
    # 不动状态机（TERMINAL / Worker 接单 CAS / 审批终态判定都不用改）
    assert outcome == "done"


async def test_halt_not_triggered_on_normal_path() -> None:
    """正常路径：halt 被 SKIPPED，`halt_triggered()` 为假。

    ⚠️ 这条与上一条**必须都在**：只测"中断时触发"是不够的——如果判据写成"有节点被跳过
    就算中断"，这里也会为真，而那条判据分不清中断与**正常跳过**（如 `test.passed == false`
    跳过 review/commit）。
    """
    ex, outcome, calls = await _run_halt_case(insufficient=False)

    assert ex.get_status("probe") == DONE
    assert ex.get_status("recap") == DONE
    assert ex.get_status("halt") == SKIPPED
    assert ex.halt_triggered() is False
    assert outcome == "done"


async def test_halt_does_not_call_the_runner() -> None:
    """halt **不经 runner**（不调 LLM）：它是确定性组装。

    "中断"是最不该出岔子的那条路——花一次 LLM 调用去做"把缺什么带出去"这件事
    既慢又可能不听话。
    """
    ex, _, calls = await _run_halt_case(insufficient=True)
    assert "halt" not in calls
    assert calls == {"scope": 1}          # probe/recap 被跳过，一次都没调


async def test_halt_output_carries_reason_and_missing() -> None:
    """halt 的输出：为什么停、缺什么——**给人和程序同一个判据**。"""
    ex, _, _ = await _run_halt_case(insufficient=True)
    out = ex.node_states["halt"]["output"]
    assert out["halted"] is True
    assert "无法定位" in out["reason"]
    assert out["missing"] == ["受影响服务", "发生时间", "错误原文"]
    assert out["triggered_by"] == ["scope"]


async def test_halt_reason_comes_from_the_triggering_upstream() -> None:
    """halt 的理由取自**触发它的那条边**的上游，而不是图里写死的某个节点。

    实测踩过：params 原先写死指向 `scope`，加了 `rca → halt` 之后，rca 触发的 run
    把 **scope 的 summary** 当成了中断理由——而那次 scope 是成功的，它在讲自己
    定位到的服务，读起来完全误导。这里用两个触发点把"取错源"钉死。
    """
    yaml_text = """
name: halt-two-triggers
version: "1.0.0"
inputs:
  bug: { type: object, required: true }
nodes:
  scope:
    agent: scope
  rca:
    agent: rca
    params: { s: "$.nodes.scope.output.summary" }
  halt:
    kind: halt
edges:
  - { from: scope, to: rca, when: "$.nodes.scope.output.insufficient == false" }
  - { from: scope, to: halt, when: "$.nodes.scope.output.insufficient == true" }
  - { from: rca, to: halt, when: "$.nodes.rca.output.insufficient == true" }
"""

    async def runner(node, params):
        if node.agent == "scope":
            return {"summary": "定位到 order-service", "insufficient": False,
                    "missing": [], "candidate_services": []}
        # 取证节点全负证据 → rca 说不出根因
        return {"summary": "各维证据全为负证据，无法确定根因类型",
                "insufficient": True, "missing": ["窗口内的错误日志原文", "故障 trace"],
                "root_cause_type": None, "confidence": 0.1,
                "hypotheses": [], "ruled_out": []}

    wf = Workflow.load_yaml(yaml_text)
    ex = DAGExecutor("run_t", "tenant-a", wf.dag, InMemoryStateStore(), node_runner=runner)
    await ex.run()

    out = ex.node_states["halt"]["output"]
    assert ex.get_status("halt") == DONE
    assert out["triggered_by"] == ["rca"]
    # ⚠️ 这正是修之前会拿到 scope 那句"定位到 order-service"的地方
    assert "负证据" in out["reason"]
    assert "定位到 order-service" not in out["reason"]
    assert out["missing"] == ["窗口内的错误日志原文", "故障 trace"]


async def test_halt_missing_normalized_to_list() -> None:
    """`missing` 归一成 `list[str]`——上游给的是字符串还是列表都收。"""
    assert DAGExecutor._as_str_list("只有一个") == ["只有一个"]
    assert DAGExecutor._as_str_list(["a", "b"]) == ["a", "b"]
    assert DAGExecutor._as_str_list(["a", " "]) == ["a"]
    assert DAGExecutor._as_str_list(None) == []
    assert DAGExecutor._as_str_list("") == []


#: 模拟"漏 gate 的那条边"：`side` 的入边是**无条件**的（对应实测里的 `know → rca`——
#: `know` 不依赖候选服务，所以它没被 gate，于是 rca 能启动、整条链跟着跑）。
HALT_LEAK_YAML = """
name: halt-leak-demo
version: "1.0.0"
inputs:
  bug: { type: object, required: true }
nodes:
  scope:
    agent: scope
  halt:
    kind: halt
    params:
      reason: "$.nodes.scope.output.summary"
  side:
    agent: side
  tail:
    agent: tail
    params: { s: "$.nodes.side.output.summary" }
edges:
  - { from: scope, to: halt, when: "$.nodes.scope.output.insufficient == true" }
  - { from: scope, to: side }
  - { from: side, to: tail }
"""


async def test_halt_skips_everything_remaining_even_with_unconditional_edges() -> None:
    """halt 触发 → **其余未执行节点全部 SKIPPED**，哪怕它们的入边是有条件的 ACTIVE。

    这条钉的是"中断"的语义边界。实测踩过：只给四个取数节点 gate 了 `insufficient`，
    可 `rca` 的 `join: any` 还有一条 `know → rca` 没 gate —— rca 照跑，plan/fix/test/
    review 全跟着跑。**逐条 gate 边这条路漏一条就前功尽弃。**

    所以"不再往下走"由 executor 统一裁决，而不是指望每张图的作者把所有通往诊断链的边
    都记得 gate 上。
    """
    wf = Workflow.load_yaml(HALT_LEAK_YAML)

    async def runner(node, params):
        if node.agent == "scope":
            return {"summary": "定位不了", "insufficient": True, "candidate_services": []}
        return {"summary": f"{node.agent}-out"}

    ex = DAGExecutor("run_leak", "tenant-a", wf.dag, InMemoryStateStore(), node_runner=runner)
    outcome = await ex.run()

    assert ex.get_status("halt") == DONE
    assert ex.halt_triggered() is True
    # ⚠️ 关键：`scope → side` 是**无条件**边（ACTIVE），但 halt 已触发 → side 必须 SKIPPED。
    # 没有这条断言，"halt 只跳过了恰好没边连着的节点"也能通过。
    assert ex.get_status("side") == SKIPPED
    assert ex.get_status("tail") == SKIPPED
    assert outcome == "done"


async def test_resume_reruns_failed_node_instead_of_preserving_it() -> None:
    """**失败节点必须能被 resume 重跑** —— 它不在 TERMINAL 里是**有意的**。

    回归背景（2026-09-21，我自己引入又撤掉的）：曾把 `FAILED` 加进 `core.dag.TERMINAL`，
    看着"失败当然是终态"很合理，但 `from_checkpoint` 正是靠**不在 TERMINAL** 把失败
    节点重置为 `pending`：

        if st.get("status") not in TERMINAL and st.get("status") != WAITING_APPROVAL:
            st = {"status": PENDING, "output": None}

    加进去之后实测：失败节点被原样保留 → 它的出边失活 → resume **直接收敛成 `done`**
    —— **一条失败的 run 续跑之后变成了"成功"**。这条测试钉住那个后果。

    注：验证时必须用 **queue 模式**建 run。inline 模式下 `start_run` 会自己起后台执行，
    两个 executor 写同一行 nodes，探针会被污染（第一版就因此得出过相反的结论）。
    """
    from agentflow.core.workflow import Workflow
    from agentflow.executor.resume import resume_executor
    from agentflow.queue.memory import InMemoryQueue
    from agentflow.service import RunService

    yaml_text = """
name: t
version: "1.0.0"
inputs: {}
nodes:
  t: { agent: tester }
edges: []
"""
    store = InMemoryStateStore()
    svc = RunService(store, queue=InMemoryQueue())    # ← queue 模式：只发布，不执行
    wf = Workflow.load_yaml(yaml_text)
    rid = (await svc.start_run("t1", wf, {}))["run_id"]

    calls = {"n": 0}

    async def bad(node, params):
        calls["n"] += 1
        return {"passed": False}

    ex = DAGExecutor(rid, "t1", wf.dag, store, node_runner=bad)
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert (await store.get_nodes(rid))["t"]["status"] == "failed"

    async def good(node, params):
        calls["n"] += 1
        return {"passed": True}

    ex2 = await resume_executor(rid, "t1", store, node_runner=good)
    assert ex2.node_states["t"]["status"] == PENDING, (
        "失败节点在 resume 时应重置为 pending（靠的就是 FAILED 不在 TERMINAL）"
    )
    assert await ex2.run() == "done"
    assert calls["n"] == 2, "失败节点必须被重跑，不是被沿用"
