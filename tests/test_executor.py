"""M2：并发 DAG 执行 + join/skip + 审批（design §8.2 / §8.3 / §8.6）。"""
from __future__ import annotations

import asyncio

import pytest

from agentflow.core.dag import DONE, SKIPPED, WAITING_APPROVAL
from agentflow.core.workflow import Workflow
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
  appr: { kind: approval, name: "审批" }
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
