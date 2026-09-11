"""B2：Ticket Inbox 端点（建/列/查/发起 run）。

夹具同 test_runs_list_api.py，另需 monkeypatch app.ticket_store。
"""
import asyncio
import time

import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app
from agentflow.api.ticket_store import TicketStore
from agentflow.api.workflow_store import WorkflowStore
from agentflow.service import RunService
from agentflow.statestore.sqlite import SqliteStateStore

# 用测试数据里的真实形状（DIAGNOSE_TEST_GUIDE §BUG）
BUG_REPORT = {
    "number": "INC0012345",
    "short_description": "订单服务结账无响应",
    "cmdb_ci": {"name": "order-service", "namespace": "order"},
    "correlation_hint": {"trace_id": "trace-20260819-143000-abc123"},
}

VALID_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug_report" } }
edges: []
"""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture
async def stores(tmp_path, monkeypatch):
    ws = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", ws)
    ts = TicketStore(tmp_path / "tickets.db")
    await ts.connect()
    monkeypatch.setattr(app_mod, "ticket_store", ts)
    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    monkeypatch.setattr(app_mod, "service", RunService(store))
    return {"workflow": ws, "ticket": ts}


async def _create(client, **over) -> dict:
    body = {
        "title": "订单服务结账无响应",
        "bug_report": BUG_REPORT,
        "window_start": "2026-08-19T14:00:00+08:00",
        "window_end": "2026-08-19T15:00:00+08:00",
    }
    body.update(over)
    resp = await client.post("/tickets", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_ticket_derives_fields_from_bug_report(stores) -> None:
    async with _client() as client:
        t = await _create(client)

    assert t["id"] and len(t["id"]) == 12
    assert t["title"] == "订单服务结账无响应"
    # number / service / namespace 从 bug_report 里兜底取
    assert t["number"] == "INC0012345"
    assert t["service"] == "order-service"
    assert t["namespace"] == "order"
    assert t["status"] == "new"
    assert t["run_ids"] == []
    # inputs 就是可直接喂给 workflow 的形状
    assert t["inputs"]["bug_report"]["number"] == "INC0012345"
    assert t["inputs"]["window_start"] == "2026-08-19T14:00:00+08:00"
    assert t["created_at"] and t["updated_at"]


async def test_explicit_fields_override_bug_report(stores) -> None:
    async with _client() as client:
        t = await _create(client, number="MANUAL-1", service="other-svc")

    assert t["number"] == "MANUAL-1"
    assert t["service"] == "other-svc"


async def test_window_omitted_when_not_given(stores) -> None:
    """没给时间窗时不写 None —— workflow 的 window_start/end 是 required，缺键报错更直白。"""
    async with _client() as client:
        t = await _create(client, window_start=None, window_end=None)

    assert "window_start" not in t["inputs"]
    assert "window_end" not in t["inputs"]


async def test_list_tickets_and_status_filter(stores) -> None:
    async with _client() as client:
        t1 = await _create(client, title="A")
        await _create(client, title="B")

        rows = (await client.get("/tickets")).json()
        assert len(rows) == 2
        assert (await client.get("/tickets?status=new")).json().__len__() == 2
        assert (await client.get("/tickets?status=resolved")).json() == []

        one = (await client.get(f"/tickets/{t1['id']}")).json()
        assert one["id"] == t1["id"]


async def test_ticket_tenant_isolation(stores) -> None:
    async with _client() as client:
        t = await _create(client)

        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        async with other:
            assert (await other.get("/tickets", headers={"X-Tenant-ID": "t2"})).json() == []
            # 跨租户取单条 → 404（不泄漏存在性）
            assert (
                await other.get(f"/tickets/{t['id']}", headers={"X-Tenant-ID": "t2"})
            ).status_code == 404


async def test_run_ticket_creates_run_and_links_back(stores) -> None:
    async with _client() as client:
        wid = (await client.post(
            "/workflows", json={"name": "simple-flow", "yaml": VALID_YAML}
        )).json()["id"]
        t = await _create(client)

        resp = await client.post(f"/tickets/{t['id']}/run", json={})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["ticket_id"] == t["id"]
        assert out["run_id"].startswith("run_")
        assert out["workflow_id"] == wid  # 未指定 → 兜底用第一个已保存流程

        # run 挂回工单，状态转为 running
        after = (await client.get(f"/tickets/{t['id']}")).json()
        assert after["run_ids"] == [out["run_id"]]
        assert after["status"] == "running"

        # 该 run 出现在 B1 的列表里，且 inputs 与工单一致
        runs = (await client.get("/runs")).json()
        assert [r["run_id"] for r in runs] == [out["run_id"]]
        assert runs[0]["inputs"]["bug_report"]["number"] == "INC0012345"


async def test_run_ticket_without_any_workflow_400(stores) -> None:
    async with _client() as client:
        t = await _create(client)
        resp = await client.post(f"/tickets/{t['id']}/run", json={})

    assert resp.status_code == 400
    assert "workflow" in resp.json()["detail"]


async def test_run_ticket_unknown_id_404(stores) -> None:
    async with _client() as client:
        resp = await client.post("/tickets/nope/run", json={})

    assert resp.status_code == 404
