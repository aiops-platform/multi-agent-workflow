"""M1：AgentScope 适配层冒烟（无 API Key 路径，ScriptedJsonModel 确定性输出）。

验证 build_toolkit（FunctionTool 包装**本地**工具）+ build_agent + run_agent 的
严格 JSON 解析（§7 输出契约）。真实 DeepSeek 路径由 S-011 冒烟覆盖（spike）。

**数据查询与 CMDB 均已迁至 MCP**（design-v5.6），本地只剩 `knowledge-lookup`
持有的 `search_knowledge`（占位）——故测试一律用它。
"""
from __future__ import annotations

from agentflow.agents.mcp import build_toolkit
from agentflow.agents.scopes import ScriptedJsonModel, build_agent, run_agent


async def test_toolkit_local_function_registered() -> None:
    """本地工具（CMDB 映射）仍进 toolkit；数据源工具已不在本地注册表。"""
    toolkit = build_toolkit("knowledge-lookup")
    schemas = await toolkit.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "search_knowledge" in names     # 仅存的本地只读工具
    assert "query_logs" not in names       # 数据源工具已迁 MCP
    assert "locate_code" not in names      # CMDB 也已迁 MCP


def test_permission_context_allow_rules_for_agent_tools() -> None:
    """§9.5：DONT_ASK + allow 规则（agent 注册工具入白名单）。"""
    from agentscope.permission import PermissionMode

    from agentflow.agents.scopes import build_permission_context

    ctx = build_permission_context("knowledge-lookup", tenant_id="team-alpha")
    assert ctx.mode == PermissionMode.DONT_ASK
    allowed = set(ctx.allow_rules.keys())
    assert "search_knowledge" in allowed  # 该 agent 注册的本地工具已入 allow
    # 未授权工具（如写类）不应在 allow 里
    assert "sandbox_run_shell" not in allowed


async def test_agent_scripted_json_roundtrip() -> None:
    """§7 输出契约：agent 输出严格 JSON 且可被 extract_json 解析。"""
    toolkit = build_toolkit("knowledge-lookup")
    model = ScriptedJsonModel(
        {"found": False, "similar_incidents": [], "summary": "无相似历史故障"}
    )
    agent = build_agent("knowledge-lookup", toolkit, model)
    out = await run_agent(agent, {"bug": "订单报价单打印失败"})
    assert out.get("found") is False
    assert out.get("summary") == "无相似历史故障"


async def test_code_locator_has_no_local_cmdb_tool() -> None:
    """CMDB 查询已迁 MCP（`locate_repo` / `get_service_topology`）——本地不再有。

    本测试是**防回退锚点**：若有人把 CMDB 又塞回本地工具，这里会失败。
    """
    from agentflow.agents.tools import build_local_tools, tools_for_agent

    # 本地工具只剩 search_knowledge（占位）
    assert {t["name"] for t in build_local_tools("code-locator")} == set()

    # 注册表里也不该再有 locate_code（它现在由 MCP server 提供）
    assert "locate_code" not in {s.name for s in tools_for_agent("code-locator")}


async def test_no_local_cmdb_provider_module() -> None:
    """§9.4 的本地 CMDB 实现已删除：agent 进程不持有服务目录数据。"""
    from agentflow.workspace.prepare import configured_repos

    # 仓库映射改由部署配置驱动（无 root 即空），不再有硬编码路径或 Mock 提供者
    assert configured_repos() == {}


# ======================================================================
# §7 输出契约：无合法 JSON 必须显式失败（曾静默返回 {}）
# ======================================================================
async def test_extract_json_no_json_returns_none() -> None:
    """extract_json 无 JSON → None；空对象 → {}（两者必须可区分）。"""
    from agentflow.agents.scopes import extract_json

    assert extract_json("") is None
    # AgentScope 轮次耗尽时 yield 的固定文本（无 JSON）
    assert extract_json(
        "Executed maximum iterations of reasoning-acting loop without finishing the task."
    ) is None
    assert extract_json("I could not finish.") is None
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_json("{}") == {}  # 空对象是合法 JSON，不等于"没有 JSON"


async def test_run_agent_raises_when_reply_has_no_json() -> None:
    """回复无 JSON → AgentOutputError，而非静默 {}。

    回归背景：extract_json 静默返回 {} 使「做了但没汇报」与「完全没做」不可区分
    （实测 committer 已完成 add+commit 却返回 {}，run 照常 success）。
    """
    import pytest

    from agentflow.agents.scopes import AgentOutputError

    toolkit = build_toolkit("knowledge-lookup")
    model = ScriptedJsonModel(
        "Executed maximum iterations of reasoning-acting loop without finishing the task."
    )
    agent = build_agent("knowledge-lookup", toolkit, model)
    with pytest.raises(AgentOutputError) as ei:
        await run_agent(agent, {"bug": "订单报价单打印失败"})
    assert ei.value.agent_name == "knowledge-lookup"
    assert "maximum iterations" in ei.value.text


async def test_on_failure_continue_degrades_to_negative_evidence() -> None:
    """on_failure: continue 的节点输出不可解析时 → 负证据，run 不中断（§8.1）。

    诊断侧负证据策略依赖此语义：单个证据 agent 拿不到结构化输出，应由
    executor 转成 {"found": false, ...} 交给下游，而非让整条链失败。
    """
    import asyncio

    from agentflow.core.workflow import Workflow
    from agentflow.executor.dag_executor import DAGExecutor
    from agentflow.statestore.memory import InMemoryStateStore

    wf = Workflow.load_yaml(
        """
name: neg-evidence
nodes:
  a: { agent: log-analyst, on_failure: continue }
  b: { agent: root-cause, upstreams: [a], on_failure: abort }
edges:
  - { from: a, to: b }
"""
    )
    store = InMemoryStateStore()

    async def raising_runner(node, params):
        """节点 a 模拟 run_agent 抛 AgentOutputError（轮次耗尽）。"""
        if node.id == "a":
            raise RuntimeError("agent 未输出合法 JSON")
        return {"summary": "rca"}

    ex = DAGExecutor("run_neg", "t", wf.dag, store, node_runner=raising_runner, inputs={})
    outcome = await asyncio.wait_for(ex.run(), timeout=5)
    assert outcome == "done"
    assert ex.get_output("a").get("found") is False  # 负证据
    assert ex.get_output("a").get("error")
    assert ex.get_status("b") == "done"
