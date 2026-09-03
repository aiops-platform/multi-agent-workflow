# -*- coding: utf-8 -*-
"""MCP server 配置 API 测试：CRUD / 校验（400 中文）/ 测试连接 / 热刷新调用。

与 test_workflow_api 同款：httpx + ASGITransport（不触发 lifespan）。
把 app.mcp_store 换成 tmp 文件存储、app.mcp_manager 换成记录型假实现（refresh/test 无副作用），
既验证端点逻辑又不真的拉起子进程。
"""
import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app
from agentflow.api.mcp_store import MCPStore

STDIO_BODY = {
    "name": "mock-mcp",
    "transport": "stdio",
    "config": {"command": "/usr/bin/python", "args": ["mock.py"]},
    "is_stateful": True,
    "enable_tools": None,
    "disable_tools": None,
    "enabled": True,
}

HTTP_BODY = {
    "name": "git-server",
    "transport": "http",
    "config": {"url": "http://127.0.0.1:8000/mcp", "headers": {"Authorization": "Bearer x"}},
    "is_stateful": False,
    "enable_tools": None,
    "disable_tools": None,
    "enabled": True,
}

_TOOLS = [
    {"name": "get_weather", "description": "查询天气", "read_only": True, "llm_name": "mcp__mock-mcp__get_weather"},
    {"name": "query.repo", "description": "查询仓库", "read_only": True, "llm_name": "mcp__mock-mcp__queryxrepo"},
    {"name": "send_alert", "description": "发告警", "read_only": False, "llm_name": "mcp__mock-mcp__send_alert"},
]


class _FakeManager:
    """记录 refresh_server 调用 + 固定 test_connection 结果的假 manager。"""

    def __init__(self) -> None:
        self.refreshed: list[str] = []
        self.closed = False
        self.test_names: list[str] = []  # 记录每次 test_connection 收到的 name（验证 name 透传）

    async def refresh_server(self, mid: str) -> None:
        self.refreshed.append(mid)

    async def close_all(self) -> None:
        self.closed = True

    async def test_connection(self, row: dict, **kwargs) -> dict:
        self.test_names.append(row.get("name"))
        url = (row.get("config") or {}).get("url", "")
        if url.startswith("http://127.0.0.1:9"):  # 连不上的目标 → ok:false
            return {"ok": False, "transport": row["transport"], "tools": [], "error": "Connection refused"}
        return {"ok": True, "transport": row["transport"], "tools": _TOOLS}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def deps(tmp_path, monkeypatch):
    store = MCPStore(tmp_path / "mcp.db")
    fake = _FakeManager()
    monkeypatch.setattr(app_mod, "mcp_store", store)
    monkeypatch.setattr(app_mod, "mcp_manager", fake)
    yield store, fake
    await store.close()  # 收掉 aiosqlite 后台线程，避免 pytest 进程退出挂起


async def test_create_returns_id_and_refreshes(deps) -> None:
    store, fake = deps
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=STDIO_BODY)
    assert resp.status_code == 201
    mid = resp.json()["id"]
    assert mid
    assert fake.refreshed == [mid]
    assert (await store.get(mid))["name"] == "mock-mcp"


async def test_create_http_default_stateless(deps) -> None:
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=HTTP_BODY)
        assert resp.status_code == 201
        row = (await client.get(f"/mcp-servers/{resp.json()['id']}")).json()
    assert row["is_stateful"] is False


async def test_create_stdio_forces_stateful(deps) -> None:
    body = {**STDIO_BODY, "is_stateful": False}  # UI 传错也强制 True
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=body)
        assert resp.status_code == 201
        row = (await client.get(f"/mcp-servers/{resp.json()['id']}")).json()
    assert row["is_stateful"] is True


async def test_create_duplicate_name_400(deps) -> None:
    async with _client() as client:
        assert (await client.post("/mcp-servers", json=STDIO_BODY)).status_code == 201
        resp = await client.post("/mcp-servers", json=STDIO_BODY)
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]


async def test_create_invalid_transport_400(deps) -> None:
    body = {**STDIO_BODY, "transport": "websocket"}
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=body)
    assert resp.status_code == 400
    assert "transport" in resp.json()["detail"]


async def test_create_stdio_missing_command_400(deps) -> None:
    body = {**STDIO_BODY, "config": {"args": []}}
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=body)
    assert resp.status_code == 400
    assert "command" in resp.json()["detail"]


async def test_create_http_missing_url_400(deps) -> None:
    body = {**HTTP_BODY, "config": {}}
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=body)
    assert resp.status_code == 400
    assert "url" in resp.json()["detail"]


async def test_create_bad_name_charset_400(deps) -> None:
    body = {**STDIO_BODY, "name": "bad name!"}
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=body)
    assert resp.status_code == 400
    assert "name" in resp.json()["detail"]


async def test_list_contains_saved(deps) -> None:
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=STDIO_BODY)).json()["id"]
        resp = await client.get("/mcp-servers")
    assert resp.status_code == 200
    assert any(r["id"] == mid for r in resp.json())


async def test_get_not_found_404(deps) -> None:
    async with _client() as client:
        resp = await client.get("/mcp-servers/nope")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


async def test_update_refreshes_and_persists(deps) -> None:
    _, fake = deps
    body = {**HTTP_BODY, "name": "git-server", "transport": "http", "is_stateful": False}
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=body)).json()["id"]
        fake.refreshed.clear()
        upd = {**body, "name": "git-renamed", "enabled": False}
        resp = await client.put(f"/mcp-servers/{mid}", json=upd)
        assert resp.status_code == 200
        assert fake.refreshed == [mid]
        row = (await client.get(f"/mcp-servers/{mid}")).json()
        assert row["name"] == "git-renamed"
        assert row["enabled"] is False


async def test_update_not_found_404(deps) -> None:
    async with _client() as client:
        resp = await client.put("/mcp-servers/nope", json=STDIO_BODY)
    assert resp.status_code == 404


async def test_delete_removes_and_refreshes(deps) -> None:
    store, fake = deps
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=STDIO_BODY)).json()["id"]
        fake.refreshed.clear()
        resp = await client.delete(f"/mcp-servers/{mid}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert fake.refreshed == [mid]
        assert await store.get(mid) is None
        assert (await client.get(f"/mcp-servers/{mid}")).status_code == 404
        assert (await client.delete(f"/mcp-servers/{mid}")).status_code == 404


async def test_test_connection_ok(deps) -> None:
    async with _client() as client:
        resp = await client.post("/mcp-servers/test", json={
            "transport": "stdio",
            "config": {"command": "python", "args": ["mock.py"]},
            "is_stateful": True,
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert {t["name"] for t in data["tools"]} == {"get_weather", "query.repo", "send_alert"}
    assert data["tools"][0]["llm_name"].startswith("mcp__")


async def test_test_connection_unreachable_ok_false(deps) -> None:
    async with _client() as client:
        resp = await client.post("/mcp-servers/test", json={
            "transport": "http",
            "config": {"url": "http://127.0.0.1:9999/mcp"},
            "is_stateful": False,
        })
    assert resp.status_code == 200  # 不崩 → 收敛为 ok:false
    assert resp.json()["ok"] is False
    assert "error" in resp.json()


async def test_test_connection_name_passthrough(deps) -> None:
    """name 透传：POST /test 带 name → client 用该名（工具 LLM 名前缀一致）；缺省 → mcp-test。"""
    _, fake = deps
    base = {"transport": "stdio", "config": {"command": "python", "args": ["mock.py"]}, "is_stateful": True}
    async with _client() as client:
        assert (await client.post("/mcp-servers/test", json=base)).status_code == 200
        assert (await client.post("/mcp-servers/test", json={**base, "name": "my-mcp"})).status_code == 200
        assert (await client.post("/mcp-servers/test", json={**base, "name": "bad name!"})).status_code == 400
    assert fake.test_names == ["mcp-test", "my-mcp"]


async def test_test_connection_invalid_transport_400(deps) -> None:
    async with _client() as client:
        resp = await client.post("/mcp-servers/test", json={
            "transport": "ssh",
            "config": {"command": "x"},
        })
    assert resp.status_code == 400


async def test_server_tools_endpoint(deps) -> None:
    _, _ = deps
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=STDIO_BODY)).json()["id"]
        # 命中 store 行 → manager.test_connection 返回 3 工具
        resp = await client.get(f"/mcp-servers/{mid}/tools")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(resp.json()["tools"]) == 3
        assert (await client.get("/mcp-servers/nope/tools")).status_code == 404


async def test_create_with_tools_persists_snapshot(deps) -> None:
    store, _ = deps
    body = {**STDIO_BODY, "tools": _TOOLS}
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=body)).json()["id"]
        row = (await client.get(f"/mcp-servers/{mid}")).json()
        listed = (await client.get("/mcp-servers")).json()
    assert row["tools"] == _TOOLS
    assert next(r for r in listed if r["id"] == mid)["tools"] == _TOOLS
    assert (await store.get(mid))["tools"] == _TOOLS


async def test_create_omitted_tools_auto_snapshot(deps) -> None:
    """create 未带 tools → best-effort 连一次填充（可达目标 → 存 _TOOLS）。"""
    store, _ = deps
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=STDIO_BODY)).json()["id"]
    assert (await store.get(mid))["tools"] == _TOOLS


async def test_create_omitted_tools_unreachable_stores_null(deps) -> None:
    """连不上 → tools=null，保存仍 201（不 500、不阻断）。"""
    store, _ = deps
    body = {**HTTP_BODY, "config": {"url": "http://127.0.0.1:9999/mcp"}, "is_stateful": False}
    async with _client() as client:
        resp = await client.post("/mcp-servers", json=body)
        assert resp.status_code == 201
        mid = resp.json()["id"]
    row = await store.get(mid)
    assert row["tools"] is None


async def test_refresh_tools_writes_back(deps) -> None:
    store, _ = deps
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=STDIO_BODY)).json()["id"]
    await store.update_tools(mid, None)  # 模拟无快照
    async with _client() as client:
        resp = await client.get(f"/mcp-servers/{mid}/tools")  # 「重新拉取」
    assert resp.json()["ok"] is True
    assert (await store.get(mid))["tools"] == _TOOLS


async def test_update_omitted_tools_preserves_snapshot(deps) -> None:
    """update 未带 tools → 保留已存快照（不 refetch、不覆盖）。"""
    store, _ = deps
    distinct = [{"name": "old_only", "read_only": True, "llm_name": "mcp__m__old_only"}]
    body = {**STDIO_BODY, "tools": distinct}
    async with _client() as client:
        mid = (await client.post("/mcp-servers", json=body)).json()["id"]
        upd = {**body, "name": "renamed-snap", "tools": None}
        resp = await client.put(f"/mcp-servers/{mid}", json=upd)
        assert resp.status_code == 200
    assert (await store.get(mid))["tools"] == distinct  # 若 refetch 会变 _TOOLS
