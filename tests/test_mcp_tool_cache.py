"""MCP 工具清单的 TTL 记忆化（`agents/mcp_tool_cache.py`）。

回归背景：上游 `list_raw_tools()` 写 `_cached_tools` 却从不读它，而 Toolkit 每轮
LLM 调用 + 每次工具执行前都会触发列举 —— 实测单次节点执行 56 个 MCP 会话里
**44 个来自这种重复列举**。
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from agentscope.mcp import HttpMCPConfig, MCPClient

from agentflow.agents.mcp_manager import MCPClientManager
from agentflow.agents.mcp_tool_cache import CachingMCPClient


def _mk(**kw: Any) -> CachingMCPClient:
    return CachingMCPClient(
        name="t", is_stateful=False,
        mcp_config=HttpMCPConfig(url="http://127.0.0.1:1/mcp"), **kw,
    )


@pytest.fixture
def calls(monkeypatch):
    """替换上游的 list_raw_tools 为计数器，避免真实网络。"""
    seen: list[int] = []

    async def fake_super(self):
        seen.append(1)
        self._cached_tools = _FAKE_TOOLS
        return self._cached_tools

    monkeypatch.setattr(MCPClient, "list_raw_tools", fake_super)
    monkeypatch.setattr("agentflow.agents.mcp_tool_cache._ttl_sec", lambda: 60.0)
    return seen


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


_FAKE_TOOLS = [_Tool("query_logs"), _Tool("get_trace"), _Tool("query_metrics")]


# ----------------------------------------------------------------------
# 记忆化本身
# ----------------------------------------------------------------------
async def test_second_call_hits_cache(calls) -> None:
    """TTL 内重复列举只有第一次真打服务端。"""
    c = _mk()
    assert len(await c.list_raw_tools()) == 3
    assert len(await c.list_raw_tools()) == 3
    assert len(await c.list_raw_tools()) == 3
    assert len(calls) == 1, f"应只列举 1 次，实际 {len(calls)}"


async def test_ttl_expiry_refetches(calls, monkeypatch) -> None:
    """TTL 过期后重新列举。"""
    c = _mk()
    await c.list_raw_tools()
    # 让缓存"变老"：直接把时间戳往回拨，避免 sleep
    c._tools_cached_at = time.monotonic() - 120
    await c.list_raw_tools()
    assert len(calls) == 2


async def test_ttl_zero_disables_cache(calls, monkeypatch) -> None:
    """TTL=0 → 关闭记忆化，退回上游行为（每次都列举）。"""
    monkeypatch.setattr("agentflow.agents.mcp_tool_cache._ttl_sec", lambda: 0.0)
    c = _mk()
    await c.list_raw_tools()
    await c.list_raw_tools()
    assert len(calls) == 2


# ----------------------------------------------------------------------
# 行为等价性（最容易出错的地方）
# ----------------------------------------------------------------------
async def test_enable_filter_kept_on_cache_hit(calls) -> None:
    """缓存命中时必须**重放过滤**，否则会把被滤掉的工具泄漏给模型。"""
    c = _mk(enable_tools=["query_logs"])
    first = await c.list_raw_tools()
    cached = await c.list_raw_tools()  # 命中缓存
    assert [t.name for t in first] == ["query_logs"]
    assert [t.name for t in cached] == ["query_logs"], "缓存命中路径未应用 enable 过滤"
    assert len(calls) == 1


async def test_disable_filter_kept_on_cache_hit(calls) -> None:
    c = _mk(disable_tools=["get_trace"])
    await c.list_raw_tools()
    cached = await c.list_raw_tools()
    assert [t.name for t in cached] == ["query_logs", "query_metrics"]
    assert len(calls) == 1


async def test_cache_holds_unfiltered_for_get_tool(calls) -> None:
    """`_cached_tools` 必须留**未过滤全集**——`get_tool` 靠它解析被滤掉的名字。"""
    c = _mk(enable_tools=["query_logs"])
    await c.list_raw_tools()
    assert {t.name for t in c._cached_tools} == {
        "query_logs", "get_trace", "query_metrics"
    }


async def test_invalidate_forces_refetch(calls) -> None:
    """重连后 invalidate → 下次重取（server 可能换版本了）。"""
    c = _mk()
    await c.list_raw_tools()
    c.invalidate_tools()
    await c.list_raw_tools()
    assert len(calls) == 2


# ----------------------------------------------------------------------
# 接入点
# ----------------------------------------------------------------------
def test_manager_builds_caching_client() -> None:
    """MCPClientManager 造出来的就是带记忆化的子类。"""
    c = MCPClientManager._build_client({
        "id": "m1", "name": "srv", "transport": "http",
        "config": {"url": "http://127.0.0.1:8300/mcp"}, "is_stateful": False,
    })
    assert isinstance(c, CachingMCPClient)
    assert isinstance(c, MCPClient)  # 仍是 MCPClient（鸭子类型不破）
