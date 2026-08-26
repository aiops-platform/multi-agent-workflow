# -*- coding: utf-8 -*-
"""M1：AgentScope 适配层冒烟（无 API Key 路径，ScriptedJsonModel 确定性输出）。

验证 build_toolkit（FunctionTool 包装 L1 工具）+ build_agent + run_agent 的
严格 JSON 解析（§7 输出契约）。真实 DeepSeek 路径由 S-011 冒烟覆盖（spike）。
"""
from __future__ import annotations

from agentflow.agents.mcp import build_toolkit
from agentflow.agents.scopes import ScriptedJsonModel, build_agent, run_agent


async def test_toolkit_l1_function_registered() -> None:
    toolkit = build_toolkit("log-analyst", use_mock=True)
    schemas = await toolkit.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    # log-analyst 的 L1 工具（§10.4）
    assert "query_logs" in names


def test_permission_context_allow_rules_for_agent_tools() -> None:
    """§9.5：DONT_ASK + allow 规则（agent 注册工具入白名单）。"""
    from agentscope.permission import PermissionBehavior, PermissionMode

    from agentflow.agents.scopes import build_permission_context

    ctx = build_permission_context("log-analyst", tenant_id="team-alpha")
    assert ctx.mode == PermissionMode.DONT_ASK
    allowed = set(ctx.allow_rules.keys())
    assert "query_logs" in allowed  # log-analyst 注册的 L1 工具已入 allow
    # 未授权工具（如写类）不应在 allow 里
    assert "sandbox_run_shell" not in allowed


async def test_agent_scripted_json_roundtrip() -> None:
    """§7 输出契约：agent 输出严格 JSON 且可被 extract_json 解析。"""
    toolkit = build_toolkit("log-analyst", use_mock=True)
    model = ScriptedJsonModel(
        {"error_type": "IOException", "error_message": "No space left", "summary": "磁盘满"}
    )
    agent = build_agent("log-analyst", toolkit, model)
    out = await run_agent(agent, {"bug": "订单报价单打印失败"})
    assert out.get("error_type") == "IOException"
    assert out.get("summary") == "磁盘满"


async def test_code_locator_cmdb_driven() -> None:
    """§9.4：code-locator 的 locate_code 走 CMDB（service → RepoSpec）。"""
    from agentflow.agents.tools import build_l1_tools
    from agentflow.workspace.cmdb import MockCmdbProvider

    cmdb = MockCmdbProvider(
        {"team-alpha": {"warranty-service": "https://github.com/company/warranty-service"}}
    )
    tools = build_l1_tools("code-locator", use_mock=True, cmdb=cmdb)
    locate = next(t for t in tools if t["name"] == "locate_code")
    out = await locate["func"](service="warranty-service")
    assert out["found"] is True
    assert out["repo_url"] == "https://github.com/company/warranty-service"

    # CMDB 未纳管的 service → found=False（负证据）
    out2 = await locate["func"](service="unknown-service")
    assert out2["found"] is False
