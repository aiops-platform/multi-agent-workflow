# -*- coding: utf-8 -*-
"""MCP 端到端集成测试：AgentNodeRunner(mcp_manager=...) 把 MCP server 注入 hybrid toolkit。

链路：MCPStore → MCPClientManager.load()（stateful stdio 连 mock server 子进程）
→ AgentNodeRunner.__call__（clients_for_agent + allow_names 预计算 + build_toolkit hybrid）
→ Agent react 循环里 LLM 首个工具调用命中 MCP 工具（read-only → 自动 ALLOW）并真实执行
→ 第二次调用把工具结果回显进最终 JSON。断言输出证明 MCP 工具真被执行（非 mock function tool）。

server 侧已无 agent 绑定（agents 字段移除）→ enabled server 对任意 agent 可见。
"""
import sys
from pathlib import Path

import pytest

from agentflow.agents.mcp_manager import MCPClientManager
from agentflow.agents.runner import AgentNodeRunner
from agentflow.agents.scopes import ScriptedJsonModel
from agentflow.api.mcp_store import MCPStore
from agentflow.core.dag import Node

MOCK_PY = Path(__file__).resolve().parents[1] / "scripts" / "mock_mcp_server.py"


def _stdio_row(name: str = "mock-mcp") -> dict:
    return {
        "name": name,
        "transport": "stdio",
        "config": {"command": sys.executable, "args": [str(MOCK_PY)], "env": None, "cwd": None},
        "is_stateful": True,
        "enable_tools": None,
        "disable_tools": None,
        "enabled": True,
    }


def _single(content_block):
    from agentscope.message import TextBlock, ToolCallBlock  # noqa: F401
    from agentscope.model import ChatResponse

    async def _gen():
        yield ChatResponse(content=[content_block], is_last=True)

    return _gen()


class _McpProbeModel(ScriptedJsonModel):
    """确定性模型：第 1 次调用请求指定 MCP 工具，第 2 次扫描历史把「是否命中 MCP 执行」回显。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__({"ok": True})
        self._tool_name = tool_name

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        from agentscope.message import TextBlock, ToolCallBlock

        self._call_count += 1
        if self._call_count == 1 and tools:
            # 精确请求 MCP 工具（llm 侧名），验证 hybrid toolkit 能执行到 MCP 会话
            return _single(ToolCallBlock(
                id=f"call_mcp_{self._call_count}", name=self._tool_name,
                input='{"city": "hangzhou"}',
            ))
        # 扫全部消息文本，看 mock server 的 get_weather 结果（weather=sunny）是否回传
        hay = []
        for m in messages:
            hay.append(str(m))
            for b in (getattr(m, "content", None) or []):
                hay.append(str(getattr(b, "text", b)))
        hit = any("weather=" in h for h in hay)
        return _single(TextBlock(
            text=f'{{"mcp_result": {"true" if hit else "false"}, "ok": true}}',
        ))


async def _manager_fixture():
    store = MCPStore(":memory:")
    mgr = MCPClientManager(store)
    await store.save(_stdio_row())
    await mgr.load()
    return mgr


async def test_allow_names_include_mcp_tools_for_any_agent() -> None:
    mgr = await _manager_fixture()
    try:
        names = await mgr.allow_names_for_agent("triage")
        assert "mcp__mock-mcp__get_weather" in names
        assert "mcp__mock-mcp__queryxrepo" in names
        # server 侧无绑定 → 别的 agent 名同样可见全部 enabled 工具
        assert "mcp__mock-mcp__get_weather" in await mgr.allow_names_for_agent("root-cause")
    finally:
        await mgr.close_all()


async def test_runner_first_tool_call_hits_mcp_tool() -> None:
    mgr = await _manager_fixture()
    runner: AgentNodeRunner | None = None
    try:
        node = Node(id="triage", agent="triage")
        runner = AgentNodeRunner(
            _McpProbeModel("mcp__mock-mcp__get_weather"),
            use_mock_datasource=True,
            mcp_manager=mgr,
        )
        out = await runner(node, {"bug": "sample"})
    finally:
        await mgr.close_all()
    # 只有 hybrid toolkit 真正执行到 MCP 会话时，get_weather 结果（weather=sunny）才会出现在历史
    assert out.get("mcp_result") is True


async def test_runner_without_mcp_manager_stays_function_tools() -> None:
    """无 mcp_manager 时行为不变：runner 正常产出（回归保护）。"""
    from agentflow.agents.scopes import ScriptedJsonModel

    node = Node(id="triage", agent="triage")
    runner = AgentNodeRunner(ScriptedJsonModel({"summary": "ok"}), use_mock_datasource=True)
    out = await runner(node, {"bug": "sample"})
    assert out.get("summary") == "ok"
