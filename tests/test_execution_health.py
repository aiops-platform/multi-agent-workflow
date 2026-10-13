"""Agent 执行健康度：继承式契约 + 引擎侧拦截。

## 这组测试要钉住的是 `run_ff42fb7bb1` 那个故障

`committer` 调了两次 `ws_open_pr`，两次都因鉴权失败；agent 如实报了
`{"pr_url":"","pr_number":0}`；而**引擎判该节点 succeeded**、run `done`、页面绿色
—— 代码从未推出去，复盘还说"已在代码侧收尾"。

根因是判据全按 **agent 名**查表（`VERDICT_FIELDS` / `ARTIFACT_FIELDS`），而 `committer`
不在任何一张表里。这组测试证明新机制**与 agent 名解耦**：判据来自 agent 自报的健康度。
"""
from __future__ import annotations

import pytest

from agentflow.agents.health import (
    HEALTH_KEY,
    ExecutionHealthMiddleware,
    attach_health,
    impact_of,
)
from agentflow.agents.mcp import build_toolkit
from agentflow.agents.runner import UsageTrackingModel
from agentflow.agents.scopes import ScriptedJsonModel, build_agent, run_agent
from agentflow.agents.tools import IMPACTS, TOOL_REGISTRY
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import DAGExecutor, WorkflowNodeFailed
from agentflow.statestore.memory import InMemoryStateStore


# ======================================================================
# 继承的落点：build_agent 无条件挂载（**不是**调用方记得传）
# ======================================================================
def _tool_call_then_json(name: str, output: dict) -> ScriptedJsonModel:
    """第 1 轮调一次工具（真实走 `on_acting`），第 2 轮输出 JSON。"""
    import json as _json

    from agentscope.message import TextBlock, ToolCallBlock
    from agentscope.model import ChatResponse

    def _one(block):
        async def _gen():
            yield ChatResponse(content=[block], is_last=True)

        return _gen()

    class _M(ScriptedJsonModel):
        def __init__(self):
            super().__init__(output)
            self._n = 0

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kw):
            self._n += 1
            if self._n == 1 and tools:
                return _one(ToolCallBlock(id="c1", name=name, input='{"query": "x"}'))
            return _one(TextBlock(text=_json.dumps(self._output_json, ensure_ascii=False)))

    return _M()


async def test_build_agent_mounts_health_middleware_and_observes_real_tool_calls() -> None:
    """**"继承"这个词的落点**：`build_agent` 自己挂，调用方**没有**传 `middlewares`。

    这条测试钉的正是它与旧机制的分界：`VERDICT_FIELDS`/`ARTIFACT_FIELDS` 要人**逐表登记**，
    所以漏一个就是一个盲区（`committer` 就是这么漏的）。而这里**工厂无条件挂** ——
    生产与测试、现有的与将来新增的 agent，一律覆盖，没有人需要记得做任何事。

    顺带证明它在**真实 agent 运行**里被调用了（不是只存在于构造参数里）——
    这是单测 `_observe` 覆盖不到的那一半：中间件钩子与 AgentScope 的对接。
    """
    toolkit = build_toolkit("knowledge-lookup")
    model = UsageTrackingModel(_tool_call_then_json("search_knowledge", {"found": False}))
    # ⚠️ 刻意**不传** middlewares —— 传了就证明不了"继承"了
    agent = build_agent("knowledge-lookup", toolkit, model)

    mw = getattr(agent, "execution_health", None)
    assert isinstance(mw, ExecutionHealthMiddleware), (
        "build_agent 没挂健康度中间件 —— 那条'每个 agent 都继承'的承诺就落空了"
    )

    await run_agent(agent, {"bug": "订单报价单打印失败"})
    assert mw.tool_calls >= 1, "中间件没被真实调用 —— 构造时挂上不等于运行时生效"


# ======================================================================
# 工具语义（impact）—— 判"目标是否达成"的依据
# ======================================================================
def test_every_registered_tool_declares_its_impact() -> None:
    """**契约测试**：TOOL_REGISTRY 里每个工具都必须显式声明 `impact`。

    这条测试是"规则可继承"这个承诺的**执行者**。机制本身不需要人登记 agent，
    所以它更不能在工具这一层靠"合理默认"漏过去 —— 漏声明的新工具会**静默走进盲区**，
    而那正是 `committer` 当初掉进去的方式（只是那次漏的是 agent，这次是工具）。

    `ToolSpec.impact` 的默认值是空串（= 未声明），所以这里能判出"漏了"。
    """
    undeclared = [n for n, s in TOOL_REGISTRY.items() if s.impact not in IMPACTS]
    assert not undeclared, (
        f"这些工具没声明 impact（取值必须是 {sorted(IMPACTS)}）：{undeclared}"
    )


def test_deliver_is_reserved_for_tools_with_no_alternative_path() -> None:
    """`deliver` 只能给**失败必 raise、且没有替代路径**的工具。

    界线很窄，因为它的判定后果最重（调用过、从未成功 ⇒ 节点失败 ⇒ 下游不执行）：

    - `ws_build_artifact` **不能**进来：它把失败**当数据返回**（`built:false`），
      `result_state` 仍是 `success` → 判 `deliver` 等于零覆盖，纯属错觉。
    - `ws_write_file` / `sandbox_write_file` **不能**进来：同一件事有替代路径
      （`sandbox_run_shell` 里 `sed -i`），判"未交付"会误伤。
    - ActionExecutor 那三个（`scale_deployment` 等）**不能**进来：生产不可达
      （`runner.py` 从不给 `build_toolkit` 传 `action_executor`），且 `remediate` 是
      `on_failure: continue` —— 标了会把一条有意的尽力而为分支变成硬中止。
    """
    deliver = {n for n, s in TOOL_REGISTRY.items() if s.impact == "deliver"}
    assert deliver == {"ws_open_pr", "ws_merge_pr", "ws_build_image"}
    for must_not in ("ws_build_artifact", "ws_write_file", "sandbox_write_file",
                     "scale_deployment", "restart_pod", "patch_resources"):
        assert TOOL_REGISTRY[must_not].impact != "deliver", must_not


def test_impact_of_reuses_read_only_for_mcp_tools() -> None:
    """MCP 工具没有 ToolSpec，但协议自带 `readOnlyHint`，流水行里已经有 `read_only`
    （`transcript.py`）—— 复用它，不新造一套元数据。

    fail-closed 方向是 `effect` 而不是 `read`：服务端没给提示（None）时当**可写**。
    """
    assert impact_of("mcp__deploy-ops__query_pods", is_mcp=True, read_only=True) == "read"
    assert impact_of("mcp__deploy-ops__rollout_deployment", is_mcp=True, read_only=False) == "effect"
    assert impact_of("mcp__deploy-ops__unknown", is_mcp=True, read_only=None) == "effect"
    # 本地工具走注册表
    assert impact_of("ws_open_pr") == "deliver"
    assert impact_of("ws_read_file") == "read"
    # 未登记的本地工具 → effect（记健康度但不判成败），不猜
    assert impact_of("某个不存在的工具") == "effect"


# ======================================================================
# 中间件：两级判据
# ======================================================================
class _Res:
    """ToolResponse 的最小替身：state + content（错误原文在 content 的 text 里）。"""

    def __init__(self, state: str, text: str = "") -> None:
        self.state = state
        self.content = [type("B", (), {"text": text})()] if text else []


class _TC:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"call_{name}"
        self.input = "{}"


class _Tool:
    def __init__(self, *, is_mcp: bool = False, read_only=None) -> None:
        self.is_mcp = is_mcp
        self.is_read_only = read_only


class _Toolkit:
    async def get_tool(self, name):
        return _Tool()


class _Agent:
    toolkit = _Toolkit()


async def _observe(mw: ExecutionHealthMiddleware, name: str, state: str, text: str = "") -> None:
    await mw._observe(_Agent(), _TC(name), _Res(state, text))


async def test_deliver_tool_called_but_never_succeeded_is_unmet() -> None:
    """**这就是那个故障的形状**：`ws_open_pr` 调了两次、都失败 → 目标未达成。

    注意判据**不看 agent 说什么** —— 模型完全可以（也确实）如实报 `pr_url: ""`，
    但那不是判据；判据是"这个工具被调用过、却从未成功"。
    """
    mw = ExecutionHealthMiddleware("committer")
    await _observe(mw, "ws_open_pr", "error", "No anonymous write access.")
    await _observe(mw, "ws_open_pr", "error", "No anonymous write access.")

    h = mw.snapshot()
    assert h is not None
    assert h["unmet_delivery"] == ["ws_open_pr"]
    assert h["degraded"] is True
    assert len(h["failed"]) == 2
    # 原始错误文本必须带出来（用户要求"要能看到错误信息"）
    assert "No anonymous write access." in h["failed"][0]["error"]
    assert h["agent"] == "committer"


async def test_deliver_succeeded_once_is_met_even_after_earlier_failure() -> None:
    """**误伤防线**：先失败一次、后来成功了 → 目标**达成**。

    agent 重试是正常的（`retry` / 换条路），判据必须是"**从未**成功过"而不是"失败过"。
    """
    mw = ExecutionHealthMiddleware("committer")
    await _observe(mw, "ws_open_pr", "error", "第一次网络抖动")
    await _observe(mw, "ws_open_pr", "success")

    h = mw.snapshot()
    assert h is not None
    assert h["unmet_delivery"] == []
    # 但那次失败仍要可见（"工具调用有错但没影响最终结果 → 警告并附错误信息"）
    assert h["degraded"] is True
    assert h["failed"][0]["error"] == "第一次网络抖动"


async def test_read_and_effect_failures_degrade_without_failing_the_goal() -> None:
    """只读/副作用类失败：**记警告，不判目标未达成**（探索时试错是常态）。"""
    mw = ExecutionHealthMiddleware("fix-implementer")
    await _observe(mw, "ws_read_file", "error", "文件不存在")
    await _observe(mw, "ws_write_file", "error", "越界")
    await _observe(mw, "sandbox_run_shell", "success")

    h = mw.snapshot()
    assert h is not None
    assert h["unmet_delivery"] == []      # 没有 deliver 工具 → 不判未达成
    assert h["degraded"] is True
    assert {f["tool"] for f in h["failed"]} == {"ws_read_file", "ws_write_file"}


async def test_all_clean_yields_no_health_at_all() -> None:
    """全绿 → `snapshot()` 返回 None，**不给输出挂空壳**（省得污染每个节点的产物）。"""
    mw = ExecutionHealthMiddleware("triage")
    await _observe(mw, "search_knowledge", "success")
    assert mw.snapshot() is None


def test_attach_health_only_wraps_dicts() -> None:
    """注入只处理 dict —— 模型可能返回标量/列表（畸形输出），那种情况不包装。"""
    h = {"degraded": True}
    assert attach_health({"a": 1}, h) == {"a": 1, HEALTH_KEY: h}
    assert attach_health("scalar", h) == "scalar"
    assert attach_health(None, h) is None
    assert attach_health({"a": 1}, None) == {"a": 1}


# ======================================================================
# DAG 引擎：异常状态的处理 + 传播
# ======================================================================
GUARD_YAML = """
name: health-guard
version: "1.0.0"
inputs: {}
nodes:
  commit:
    agent: committer
edges: []
"""


class _HealthRunner:
    """脚本化 runner：固定输出（**已经带 _health**，模拟 runner 出口的注入）。"""

    def __init__(self, output: dict) -> None:
        self.output = output

    async def __call__(self, node, params) -> dict:
        return dict(self.output)


def _commit_output(*, unmet: list[str], failed: list[dict]) -> dict:
    return {
        "pr_url": "",
        "pr_number": 0,
        HEALTH_KEY: {
            "agent": "committer",
            "tool_calls": len(failed),
            "failed": failed,
            "unmet_delivery": unmet,
            "degraded": bool(failed),
        },
    }


async def test_unmet_delivery_fails_the_node_and_stops_the_run() -> None:
    """承诺交付没做成 ⇒ 节点 FAILED ⇒ run FAILED ⇒ **下游不执行**（蔓延被切断）。"""
    wf = Workflow.load_yaml(GUARD_YAML)
    ex = DAGExecutor(
        "run_h1", "t", wf.dag, InMemoryStateStore(),
        node_runner=_HealthRunner(_commit_output(
            unmet=["ws_open_pr"],
            failed=[{"tool": "ws_open_pr", "impact": "deliver", "state": "error",
                     "error": "remote: No anonymous write access."}],
        )),
    )
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()

    assert ex.get_status("commit") == "failed"
    msg = str(ei.value)
    # 工具名与**原始错误文本**都要看得见（用户要求）
    assert "ws_open_pr" in msg
    assert "No anonymous write access." in msg
    # 失败节点也带健康度（injection 点在 DONE 分支，致命失败在此之前就 raise 了，
    # 所以必须显式随 WorkflowNodeFailed.output 一起带出去）
    assert (ei.value.output or {}).get(HEALTH_KEY, {}).get("unmet_delivery") == ["ws_open_pr"]


async def test_degraded_but_delivered_node_stays_done() -> None:
    """有工具失败、但**目标达成** ⇒ 节点 **done**，且健康度随输出传给下游与 DB。

    这正是用户要的"警告并附上错误信息，然后继续"。
    """
    wf = Workflow.load_yaml(GUARD_YAML)
    store = InMemoryStateStore()
    ex = DAGExecutor(
        "run_h2", "t", wf.dag, store,
        node_runner=_HealthRunner({
            "pr_url": "https://github.com/x/y/pull/7",
            "pr_number": 7,
            HEALTH_KEY: {
                "agent": "committer", "tool_calls": 3,
                "failed": [{"tool": "ws_git", "impact": "effect", "state": "error",
                            "error": "nothing to commit"}],
                "unmet_delivery": [], "degraded": True,
            },
        }),
    )
    await ex.run()
    assert ex.get_status("commit") == "done"
    # 健康度确实进了节点状态（下游 params 与 API 都走这条路）
    assert ex.node_states["commit"]["output"][HEALTH_KEY]["degraded"] is True


async def test_guard_is_a_noop_when_health_is_absent() -> None:
    """**判不了就不判**：没有 `_health`（mock runner / 旧数据）→ 放行。不猜。"""
    wf = Workflow.load_yaml(GUARD_YAML)
    ex = DAGExecutor(
        "run_h3", "t", wf.dag, InMemoryStateStore(),
        node_runner=_HealthRunner({"pr_url": "", "pr_number": 0}),
    )
    await ex.run()
    assert ex.get_status("commit") == "done"


async def test_delivery_guard_ignores_on_failure_continue() -> None:
    """`on_failure: continue` **不能**把它变回 DONE —— 与另两个守卫同一个理由：
    放 runner 里抛会走 `on_error` → `continue` 转成**负证据**、节点照样标 DONE，
    而那正是要避免的"看着成功"。
    """
    yaml_text = GUARD_YAML.replace(
        "  commit:\n    agent: committer",
        "  commit:\n    agent: committer\n    on_failure: continue",
    )
    assert "on_failure: continue" in yaml_text, "替换没生效，测的就不是这条规则了"
    wf = Workflow.load_yaml(yaml_text)
    ex = DAGExecutor(
        "run_h4", "t", wf.dag, InMemoryStateStore(),
        node_runner=_HealthRunner(_commit_output(
            unmet=["ws_open_pr"],
            failed=[{"tool": "ws_open_pr", "impact": "deliver", "state": "error", "error": "鉴权失败"}],
        )),
    )
    with pytest.raises(WorkflowNodeFailed):
        await ex.run()
    assert ex.get_status("commit") == "failed"


async def test_delivery_guard_runs_before_the_verdict_guard() -> None:
    """**顺序**：交付守卫排在 verdict 守卫**之前**，否则原始工具错误文本会丢。

    `merger` 会同时命中两者：`merged=false`（verdict）与 `ws_merge_pr` 从未成功（delivery）。
    verdict 守卫的 message 用的是 agent 的**转述**（`output["summary"]`）——
    先跑它，message 里就只剩转述，**看不到原始错误**。而恰恰这两个 agent 最需要那原文。
    """
    yaml_text = GUARD_YAML.replace("agent: committer", "agent: merger")
    wf = Workflow.load_yaml(yaml_text)
    ex = DAGExecutor(
        "run_h5", "t", wf.dag, InMemoryStateStore(),
        node_runner=_HealthRunner({
            "merged": False,                      # ← verdict 守卫也会命中
            "summary": "合并没成功",               # ← agent 的转述
            HEALTH_KEY: {
                "agent": "merger", "tool_calls": 1,
                "failed": [{"tool": "ws_merge_pr", "impact": "deliver", "state": "error",
                            "error": "GraphQL: Pull Request is not mergeable (422)"}],
                "unmet_delivery": ["ws_merge_pr"], "degraded": True,
            },
        }),
    )
    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()

    msg = str(ei.value)
    assert "ws_merge_pr" in msg
    assert "Pull Request is not mergeable" in msg, "原始错误文本丢了 —— 守卫顺序反了"
