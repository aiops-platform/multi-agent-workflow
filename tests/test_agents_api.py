"""API 控制面测试：GET /agents（静态 agent 编队列表）。

用 httpx.AsyncClient + ASGITransport 驱动 FastAPI 应用——ASGITransport **不触发**
lifespan startup，因此不会 init() / 建 data/agentflow.db / 启动 ApprovalSweeper，
测试无副作用。/agents 只读 AGENT_REGISTRY，不依赖 service。
"""
import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.agents.registry import DIAGNOSE_AGENTS, FIX_AGENTS
from agentflow.api.agent_store import AgentConfigStore
from agentflow.api.app import app
from agentflow.api.mcp_store import MCPStore


def _client() -> httpx.AsyncClient:
    # base_url 必须给：httpx 用相对 URL 时 cookie 解析需要绝对 URL（ASGITransport 场景）
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture(autouse=True)
async def _sqlite_control_stores(tmp_path, monkeypatch):
    """把控制面 store 换成临时 sqlite。

    B3 起 ``/agents`` 要读 agent 配置与 MCP 绑定（推 tier 与 MCP 工具面），不再像
    改造前那样只读静态注册表。而 app.py 是在 **import 时**用当时的 settings 构造模块
    全局 store 的 —— 本机 .env 若是 ``state_store=postgres``，那全局就是 Pg* 实现，
    测试里一碰就 ImportError（缺 psycopg）。``test_agent_config_api.deps`` 早已这么做，
    这里跟随同一模式。
    """
    monkeypatch.setattr(app_mod, "agent_config_store", AgentConfigStore(tmp_path / "cfg.db"))
    monkeypatch.setattr(app_mod, "mcp_store", MCPStore(tmp_path / "mcp.db"))
    monkeypatch.setattr(app_mod, "_agent_config_resolver", None)


async def test_agents_returns_all_15_in_registry_order() -> None:
    async with _client() as client:
        resp = await client.get("/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 15
    assert [a["name"] for a in data] == DIAGNOSE_AGENTS + FIX_AGENTS


async def test_agents_item_shape_and_real_tools() -> None:
    async with _client() as client:
        resp = await client.get("/agents")
    data = resp.json()
    for item in data:
        # B3 起字段扩充（供 Agent 目录的 Inspector 用）。断言必需键齐备而非精确集合，
        # 以免后续再加字段就打破这个用例。
        assert {
            "name", "description", "stage", "role", "enabled", "origin",
            "reasoning_enabled", "mcp_server_ids", "bound_servers",
            "local_tools", "mcp_tools", "tools", "tool_count",
        } <= set(item)
        assert isinstance(item["description"], str) and item["description"]
        assert item["role"] in {"diagnose", "fix"}
        assert isinstance(item["stage"], str) and item["stage"]
        # local_tools 必须带 level / needs_approval —— 前端靠这两个字段推导自治 tier
        for t in item["local_tools"]:
            assert set(t) == {"name", "level", "needs_approval", "description"}
            assert t["level"] in {"L1", "L2"}
            assert isinstance(t["needs_approval"], bool)
    # 数据源工具已迁 MCP（design-v5.6），本地注册表仅剩 CMDB 映射与知识检索。
    # local_tools 反映的是**本地**注册表：数据查询与 CMDB 均已迁 MCP，
    # 本地只剩 knowledge-lookup 的 search_knowledge（占位）。
    triage = next(a for a in data if a["name"] == "triage")
    assert triage["stage"] == "detect"
    assert triage["local_tools"] == []
    assert triage["tools"] == []

    know = next(a for a in data if a["name"] == "knowledge-lookup")
    assert [t["name"] for t in know["local_tools"]] == ["search_knowledge"]

    # committer 的 ws_git 是 L2 且需审批 —— 前端据此推导 T3+ 半自动
    commit = next(a for a in data if a["name"] == "committer")
    ws_git = next(t for t in commit["local_tools"] if t["name"] == "ws_git")
    assert ws_git["level"] == "L2"
    assert ws_git["needs_approval"] is True


async def test_agent_detail_has_prompt_and_schema() -> None:
    """``GET /agents/{name}`` 是 Inspector 的数据源：完整 prompt + 输出契约 + 覆盖状态。"""
    async with _client() as client:
        resp = await client.get("/agents/triage")
    assert resp.status_code == 200
    d = resp.json()

    assert d["name"] == "triage"
    assert d["role"] == "diagnose"
    assert d["stage"] == "detect"
    # 完整 system_prompt（列表端点只有一句话 description）
    assert isinstance(d["system_prompt"], str) and len(d["system_prompt"]) > 50
    # 输出契约
    assert d["schema"]["required"]  # BugReportSchema
    # stored 全 null = 无 DB 覆盖（用内置默认）
    assert d["stored"] == {"description": None, "system_prompt": None, "schema": None}


async def test_agent_detail_unknown_404() -> None:
    async with _client() as client:
        resp = await client.get("/agents/no-such-agent")
    assert resp.status_code == 404


async def test_agents_stats_empty_window(tmp_path, monkeypatch) -> None:
    """没有 run 时返回空聚合，**各值是 None 而不是 0** —— 前端要能区分「没跑过」与「跑了但为 0」。

    这个用例同时覆盖路由顺序：``/agents/stats`` 若被 ``/agents/{name}`` 抢先匹配，
    会因为 "stats" 不是 agent 而返回 404（同 ``/workflows/preview`` 的教训）。
    """
    import agentflow.api.app as app_mod
    from agentflow.service import RunService
    from agentflow.statestore.sqlite import SqliteStateStore

    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    monkeypatch.setattr(app_mod, "service", RunService(store))

    async with _client() as client:
        resp = await client.get("/agents/stats")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"window_runs": 0, "agents": {}}
