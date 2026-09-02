# -*- coding: utf-8 -*-
"""workflow CRUD API 测试：/workflows 保存/列表/读取/更新/删除 + /workflows/preview 预览。

与 test_cors.py / test_agents_api.py 同款：httpx.AsyncClient + ASGITransport（不触发 lifespan）。
通过 monkeypatch 把 app.workflow_store 替换为指向 tmp_path 的 WorkflowStore，
避免污染 data/agentflow.db，也无需 init()。
"""
import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app
from agentflow.api.workflow_store import WorkflowStore

# 合法 workflow（multi-agent-workflow DAG 语义：显式边 + params 仅引用传递上游）
VALID_YAML = """
name: test-flow
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug_report" } }
  rca:    { agent: root-cause, params: { code: "$.nodes.triage.output.summary" } }
edges:
  - { from: triage, to: rca }
"""

# 含审批节点的 workflow（kind: approval，agent 应为 None）
APPROVAL_YAML = """
name: approval-flow
nodes:
  triage: { agent: triage }
  approve-changes:
    kind: approval
    approvers: [lead-engineer]
    timeout: 3600
edges:
  - { from: triage, to: approve-changes }
"""

# DAG 静态校验失败：边指向不存在的节点 → WorkflowDAGError
INVALID_YAML = """
name: broken
nodes:
  triage: { agent: triage }
edges:
  - { from: triage, to: missing }
"""


def _client() -> httpx.AsyncClient:
    # base_url 必须给：httpx 用相对 URL 时 cookie 解析需要绝对 URL（ASGITransport 场景）
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture
async def ws(tmp_path, monkeypatch):
    """把 app.workflow_store 替换为临时文件存储（每测试独立，互不污染）。"""
    store = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", store)
    return store


async def _create(client, yaml_text: str, name: str = "test-flow") -> httpx.Response:
    return await client.post(
        "/workflows", json={"name": name, "yaml": yaml_text}
    )


async def test_create_workflow_returns_id_and_graph(ws) -> None:
    async with _client() as client:
        resp = await _create(client, VALID_YAML)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"]
    assert data["name"] == "test-flow"
    graph = data["graph"]
    assert graph["name"] == "test-flow"
    assert {n["id"] for n in graph["nodes"]} == {"triage", "rca"}
    assert {n["agent"] for n in graph["nodes"]} == {"triage", "root-cause"}
    assert graph["edges"] == [{"from": "triage", "to": "rca", "when": None}]


async def test_approval_node_kind_in_graph(ws) -> None:
    async with _client() as client:
        resp = await _create(client, APPROVAL_YAML, name="approval-flow")
    assert resp.status_code == 200
    nodes = resp.json()["graph"]["nodes"]
    by_id = {n["id"]: n for n in nodes}
    assert by_id["approve-changes"]["kind"] == "approval"
    assert by_id["approve-changes"]["agent"] is None
    assert by_id["triage"]["kind"] == "agent"


async def test_create_invalid_yaml_returns_400(ws) -> None:
    async with _client() as client:
        resp = await _create(client, INVALID_YAML)
    assert resp.status_code == 400
    assert "解析失败" in resp.json()["detail"]


async def test_list_workflows_contains_saved(ws) -> None:
    async with _client() as client:
        created = (await _create(client, VALID_YAML)).json()
        resp = await client.get("/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert any(w["id"] == created["id"] for w in data)


async def test_get_workflow_returns_yaml_and_graph(ws) -> None:
    async with _client() as client:
        created = (await _create(client, VALID_YAML)).json()
        resp = await client.get(f"/workflows/{created['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == created["id"]
    assert "name:" in data["yaml"]
    assert data["graph"]["name"] == "test-flow"


async def test_get_workflow_not_found_404(ws) -> None:
    async with _client() as client:
        resp = await client.get("/workflows/nope")
    assert resp.status_code == 404


async def test_update_workflow(ws) -> None:
    async with _client() as client:
        created = (await _create(client, VALID_YAML)).json()
        resp = await client.put(
            f"/workflows/{created['id']}",
            json={"name": "renamed", "yaml": VALID_YAML},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"
        got = (await client.get(f"/workflows/{created['id']}")).json()
        assert got["name"] == "renamed"


async def test_update_workflow_not_found_404(ws) -> None:
    async with _client() as client:
        resp = await client.put(
            "/workflows/nope", json={"name": "x", "yaml": VALID_YAML}
        )
    assert resp.status_code == 404


async def test_delete_workflow(ws) -> None:
    async with _client() as client:
        created = (await _create(client, VALID_YAML)).json()
        resp = await client.delete(f"/workflows/{created['id']}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert (await client.get(f"/workflows/{created['id']}")).status_code == 404
        assert (await client.delete(f"/workflows/{created['id']}")).status_code == 404


async def test_preview_returns_graph_without_saving(ws) -> None:
    async with _client() as client:
        resp = await client.post("/workflows/preview", json={"yaml": VALID_YAML})
        assert resp.status_code == 200
        assert resp.json()["graph"]["name"] == "test-flow"
        # 预览不落库
        assert (await client.get("/workflows")).json() == []
