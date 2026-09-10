"""API 控制面测试：GET /agents（静态 agent 编队列表）。

用 httpx.AsyncClient + ASGITransport 驱动 FastAPI 应用——ASGITransport **不触发**
lifespan startup，因此不会 init() / 建 data/agentflow.db / 启动 ApprovalSweeper，
测试无副作用。/agents 只读 AGENT_REGISTRY，不依赖 service。
"""
import httpx

from agentflow.agents.registry import DIAGNOSE_AGENTS, FIX_AGENTS
from agentflow.api.app import app


def _client() -> httpx.AsyncClient:
    # base_url 必须给：httpx 用相对 URL 时 cookie 解析需要绝对 URL（ASGITransport 场景）
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


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
        assert set(item) == {"name", "description", "tools", "stage"}
        assert isinstance(item["description"], str) and item["description"]
        assert isinstance(item["tools"], list)
        assert all(isinstance(t, str) for t in item["tools"])
        assert isinstance(item["stage"], str) and item["stage"]
    # 数据源工具已迁 MCP（design-v5.5），本地注册表仅剩 CMDB 映射与知识检索。
    # /agents 的 tools 反映的是**本地**注册表，故 code-locator/knowledge-lookup 有工具，
    # 而 triage 这类纯取数 agent 不再有本地工具（其 get_trace 现由 MCP 提供）。
    triage = next(a for a in data if a["name"] == "triage")
    assert triage["stage"] == "detect"
    assert triage["tools"] == []

    locator = next(a for a in data if a["name"] == "code-locator")
    assert "locate_code" in locator["tools"]
