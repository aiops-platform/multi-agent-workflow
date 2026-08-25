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
