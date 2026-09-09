"""AgentSpec 配置 API 测试：CRUD / 400 中文校验 / bound_servers join / GET /agents 合并视图。

httpx + ASGITransport（不触发 lifespan → 不 init()/seed/起 Sweeper）。monkeypatch app 模块全局：
- agent_config_store → tmp 文件 store（隔离 CRUD 副作用，store 空 = 未 seed 状态）；
- mcp_store → 假 store（仅 get()，供 bound_servers join；不真起连接）；
- _agent_config_resolver → None（CRUD 端点内部会重建它并重接 mcp_manager.server_ids_for；
  monkeypatch teardown 还原为 None，避免残留 resolver 污染其它文件对 GET /agents 静态 15 的断言）。
"""
import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.agents.agent_config import AgentConfigResolver
from agentflow.agents.prompts import SYSTEM_PROMPTS
from agentflow.agents.registry import AGENT_DESCRIPTIONS, DIAGNOSE_AGENTS, FIX_AGENTS
from agentflow.api.agent_store import AgentConfigStore
from agentflow.api.app import app

CUSTOM = {
    "name": "custom-diag",
    "role": "diagnose",
    "stage": "diagnose",
    "description": "自定义诊断体",
    "system_prompt": "你是深度诊断体，输出 JSON。",
    "enabled": True,
}


class _FakeMcpStore:
    """仅实现 bound_servers join 需要的 get()：m1 在库，ghost 是残留引用。"""

    _servers = {"m1": {"id": "m1", "name": "git-tools", "transport": "http"}}

    async def get(self, mid: str):
        return self._servers.get(mid)


def _builtin_row(name: str = "triage", **over) -> dict:
    row = {
        "name": name,
        "origin": "builtin",
        "role": "diagnose",
        "stage": "detect",
        "description": None,
        "system_prompt": None,
        "schema": None,
        "mcp_server_ids": None,
        "enabled": True,
        "reasoning_enabled": False,
    }
    row.update(over)
    return row


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def deps(tmp_path, monkeypatch):
    store = AgentConfigStore(tmp_path / "cfg.db")
    monkeypatch.setattr(app_mod, "agent_config_store", store)
    monkeypatch.setattr(app_mod, "mcp_store", _FakeMcpStore())
    monkeypatch.setattr(app_mod, "_agent_config_resolver", None)  # 端点重建后 teardown 还原
    yield store
    await store.close()


# ---- POST 新建 custom ----
async def test_create_custom_201(deps) -> None:
    async with _client() as client:
        resp = await client.post("/agent-configs", json=CUSTOM)
    assert resp.status_code == 201
    assert resp.json() == {"name": "custom-diag", "origin": "custom"}
    assert (await deps.get("custom-diag"))["origin"] == "custom"


async def test_create_custom_with_binding_and_schema_roundtrip(deps) -> None:
    body = {**CUSTOM, "mcp_server_ids": ["m1"], "output_schema": {"type": "object", "properties": {}}}
    async with _client() as client:
        assert (await client.post("/agent-configs", json=body)).status_code == 201
        detail = (await client.get("/agent-configs/custom-diag")).json()
    assert detail["mcp_server_ids"] == ["m1"]
    assert detail["schema"] == {"type": "object", "properties": {}}
    assert detail["bound_servers"] == [{"id": "m1", "name": "git-tools", "transport": "http"}]


async def test_create_duplicate_name_400(deps) -> None:
    async with _client() as client:
        assert (await client.post("/agent-configs", json=CUSTOM)).status_code == 201
        resp = await client.post("/agent-configs", json=CUSTOM)
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]


async def test_create_builtin_name_400(deps) -> None:
    """撞内置名 → 400 引导去编辑（POST 只建 custom）。"""
    async with _client() as client:
        resp = await client.post("/agent-configs", json={**CUSTOM, "name": "triage"})
    assert resp.status_code == 400
    assert "内置" in resp.json()["detail"]


async def test_create_bad_name_charset_400(deps) -> None:
    async with _client() as client:
        resp = await client.post("/agent-configs", json={**CUSTOM, "name": "bad name"})
        assert resp.status_code == 400
        assert "name" in resp.json()["detail"]
        resp2 = await client.post("/agent-configs", json={**CUSTOM, "name": "a.b"})
        assert resp2.status_code == 400
        assert "name" in resp2.json()["detail"]


async def test_create_missing_role_400(deps) -> None:
    body = {k: v for k, v in CUSTOM.items() if k != "role"}
    async with _client() as client:
        resp = await client.post("/agent-configs", json=body)
    assert resp.status_code == 400
    assert "role" in resp.json()["detail"]


async def test_create_invalid_role_and_stage_400(deps) -> None:
    async with _client() as client:
        resp = await client.post("/agent-configs", json={**CUSTOM, "role": "operator"})
        assert resp.status_code == 400
        assert "role" in resp.json()["detail"]
        resp2 = await client.post("/agent-configs", json={**CUSTOM, "stage": "bogus"})
        assert resp2.status_code == 400
        assert "stage" in resp2.json()["detail"]


async def test_create_missing_system_prompt_400(deps) -> None:
    body = {**CUSTOM, "system_prompt": ""}
    async with _client() as client:
        resp = await client.post("/agent-configs", json=body)
    assert resp.status_code == 400
    assert "system_prompt" in resp.json()["detail"]


# ---- GET 列表 / 单条 ----
async def test_list_and_get_detail(deps) -> None:
    async with _client() as client:
        await client.post("/agent-configs", json=CUSTOM)
        listed = (await client.get("/agent-configs")).json()
        detail = (await client.get("/agent-configs/custom-diag")).json()
    row = next(r for r in listed if r["name"] == "custom-diag")
    assert row["origin"] == "custom"
    assert row["effective_description"] == CUSTOM["description"]
    # 未指定 mcp_server_ids → NULL → 无绑定（两态：没配置就没有 server）→ bound_servers = []
    assert row["bound_servers"] == []
    assert detail["role"] == "diagnose"
    assert detail["description"] == CUSTOM["description"]
    assert detail["system_prompt"] == CUSTOM["system_prompt"]
    assert detail["mcp_server_ids"] is None
    assert detail["stored"]["description"] == CUSTOM["description"]


async def test_get_not_found_404(deps) -> None:
    async with _client() as client:
        resp = await client.get("/agent-configs/nope")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


# ---- PUT 覆盖/清空（回退内置）----
async def test_put_builtin_override_and_clear_fallback(deps) -> None:
    await deps.save(_builtin_row())  # triage 内置行（seed 等价）
    async with _client() as client:
        r = await client.put("/agent-configs/triage", json={
            "description": "新描述", "system_prompt": "新提示", "mcp_server_ids": ["m1"],
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True, "name": "triage"}
        detail = (await client.get("/agent-configs/triage")).json()
    assert detail["description"] == "新描述"
    assert detail["system_prompt"] == "新提示"
    assert detail["mcp_server_ids"] == ["m1"]
    assert detail["bound_servers"] == [{"id": "m1", "name": "git-tools", "transport": "http"}]
    assert detail["stored"]["description"] == "新描述"
    assert detail["origin"] == "builtin"

    # 清空覆盖 + 清空绑定（无 server，两态）→ 详情回退内置静态默认
    async with _client() as client:
        r2 = await client.put("/agent-configs/triage", json={
            "description": "", "system_prompt": None, "mcp_server_ids": None,
        })
        assert r2.status_code == 200
        detail2 = (await client.get("/agent-configs/triage")).json()
    assert detail2["description"] == AGENT_DESCRIPTIONS["triage"]
    assert detail2["system_prompt"] == SYSTEM_PROMPTS["triage"]
    assert detail2["stored"]["description"] is None
    assert detail2["bound_servers"] == []  # 无绑定 server
    assert (await deps.get("triage"))["system_prompt"] is None  # 库中已清成 NULL


async def test_put_custom_requires_nonempty_system_prompt(deps) -> None:
    await deps.save(_builtin_row(name="custom-diag", origin="custom", system_prompt="sp", stage="diagnose"))
    async with _client() as client:
        resp = await client.put("/agent-configs/custom-diag", json={"system_prompt": ""})
    assert resp.status_code == 400
    assert "system_prompt" in resp.json()["detail"]


async def test_put_not_found_404(deps) -> None:
    async with _client() as client:
        resp = await client.put("/agent-configs/nope", json={"system_prompt": "x"})
    assert resp.status_code == 404


async def test_put_disables_agent(deps) -> None:
    await deps.save(_builtin_row())
    async with _client() as client:
        assert (await client.put("/agent-configs/triage", json={"enabled": False})).status_code == 200
        detail = (await client.get("/agent-configs/triage")).json()
    assert detail["enabled"] is False


async def test_put_and_create_reasoning_enabled_roundtrip(deps) -> None:
    """reasoning_enabled 开关：PUT 内置行置 true → GET 回显；POST custom 指定 / 缺省正确。"""
    await deps.save(_builtin_row())
    async with _client() as client:
        # PUT 置 true → GET 回显 true
        assert (await client.put("/agent-configs/triage", json={"reasoning_enabled": True})).status_code == 200
        assert (await client.get("/agent-configs/triage")).json()["reasoning_enabled"] is True
        # 显式回 false → GET 回显 false
        assert (await client.put("/agent-configs/triage", json={"reasoning_enabled": False})).status_code == 200
        assert (await client.get("/agent-configs/triage")).json()["reasoning_enabled"] is False
        # POST custom 缺省 → false；列表经 store.list() 展开 → 字段透出
        assert (await client.post("/agent-configs", json=CUSTOM)).status_code == 201
        assert (await client.get("/agent-configs/custom-diag")).json()["reasoning_enabled"] is False
        listed = (await client.get("/agent-configs")).json()
    row = next(r for r in listed if r["name"] == "triage")
    assert row["reasoning_enabled"] is False


# ---- DELETE ----
async def test_delete_builtin_400(deps) -> None:
    await deps.save(_builtin_row())
    async with _client() as client:
        resp = await client.delete("/agent-configs/triage")
    assert resp.status_code == 400
    assert "不可删除" in resp.json()["detail"]
    assert (await deps.get("triage")) is not None


async def test_delete_custom_200_then_404(deps) -> None:
    async with _client() as client:
        assert (await client.post("/agent-configs", json=CUSTOM)).status_code == 201
        resp = await client.delete("/agent-configs/custom-diag")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert (await client.get("/agent-configs/custom-diag")).status_code == 404
        assert (await client.delete("/agent-configs/custom-diag")).status_code == 404
    assert await deps.get("custom-diag") is None


async def test_delete_not_found_404(deps) -> None:
    async with _client() as client:
        resp = await client.delete("/agent-configs/nope")
    assert resp.status_code == 404


# ---- bound_servers 残留引用 ----
async def test_bound_servers_residual_reference(deps) -> None:
    """绑定里引用了已删除 server → 兜底显示 {id, name=id, transport:'?'}（不 500）。"""
    async with _client() as client:
        await client.post("/agent-configs", json={**CUSTOM, "mcp_server_ids": ["m1", "ghost"]})
        detail = (await client.get("/agent-configs/custom-diag")).json()
    assert detail["bound_servers"] == [
        {"id": "m1", "name": "git-tools", "transport": "http"},
        {"id": "ghost", "name": "ghost", "transport": "?"},
    ]


# ---- GET /agents 合并视图（DB 覆盖 + 自定义并入内置 15）----
async def test_get_agents_reflects_override_and_custom(deps, monkeypatch) -> None:
    await deps.save(_builtin_row(description="覆盖描述"))
    await deps.save(_builtin_row(
        name="custom-x", origin="custom", role="diagnose", stage="other",
        description="自定", system_prompt="sp",
    ))
    monkeypatch.setattr(app_mod, "_agent_config_resolver", AgentConfigResolver(await deps.list()))
    async with _client() as client:
        data = (await client.get("/agents")).json()
    assert [a["name"] for a in data] == DIAGNOSE_AGENTS + FIX_AGENTS + ["custom-x"]
    assert len(data) == 16
    triage = next(a for a in data if a["name"] == "triage")
    assert triage["description"] == "覆盖描述"
    custom = data[-1]
    assert custom["tools"] == []  # 自定义 agent 无 L1 函数工具（仅可绑 MCP）
    assert set(custom) == {"name", "description", "tools", "stage"}


async def test_get_agents_without_resolver_stays_static_15(deps) -> None:
    """store 空（未 init / 未 seed）→ GET /agents == 纯内置 15（既有 shape 不破）。"""
    async with _client() as client:
        data = (await client.get("/agents")).json()
    assert [a["name"] for a in data] == DIAGNOSE_AGENTS + FIX_AGENTS
    assert all(set(a) == {"name", "description", "tools", "stage"} for a in data)
