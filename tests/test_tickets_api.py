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


# ──────────────────────────────────────────────────────────────────
# 幂等建单（`create_once`）：`kind: ticket` 节点的落点
# ──────────────────────────────────────────────────────────────────
async def test_create_once_returns_the_existing_ticket_for_same_source_ref(tmp_path) -> None:
    """同一 `source_ref` 建两次 → **只有一张**，第二次拿到的是同一张（`created=False`）。

    这是「一条数据永远只有一张工单」这个不变量的落点。它比引擎的
    `external_operation_id` 强：后者只活在 attempt 账本里，管得住"同 run 重放"，
    管不住"同一条数据被两个 run / 两次手工重跑各建一张"。
    """
    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    a, ca = await ts.create_once("otr", source_ref="PR-0001", title="t1", inputs={"x": 1})
    b, cb = await ts.create_once("otr", source_ref="PR-0001", title="t1-again", inputs={"x": 2})
    assert ca is True and cb is False
    assert a["id"] == b["id"]
    assert a["number"] == b["number"]          # 第二次没有另取号
    assert b["title"] == "t1"                  # 也没有被覆盖
    assert len(await ts.list("otr")) == 1


async def test_create_once_is_race_safe(tmp_path) -> None:
    """并发建同一条 → 仍只有一张（唯一索引 + DO NOTHING + 回读兜底）。"""
    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    results = await asyncio.gather(
        *[ts.create_once("otr", source_ref="PR-0002", title="c", inputs={}) for _ in range(8)]
    )
    assert len({row["id"] for row, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    assert len(await ts.list("otr")) == 1


async def test_create_once_is_scoped_per_tenant(tmp_path) -> None:
    """同一个 `source_ref` 在两个租户下是**两张**单（`source_ref` 是租户内的号）。"""
    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    a, ca = await ts.create_once("otr", source_ref="PR-0003", title="a", inputs={})
    b, cb = await ts.create_once("local", source_ref="PR-0003", title="b", inputs={})
    assert ca is True and cb is True and a["id"] != b["id"]


async def test_next_number_format_and_is_per_day(tmp_path) -> None:
    """工单号 `INC-YYYYMMDD-NNNN`，同日自增、跨日归 1。"""
    from datetime import UTC, datetime

    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    d1 = datetime(2026, 9, 21, tzinfo=UTC)
    d2 = datetime(2026, 9, 22, tzinfo=UTC)
    assert await ts.next_number(now=d1) == "INC-20260921-0001"
    assert await ts.next_number(now=d1) == "INC-20260921-0002"
    assert await ts.next_number(now=d2) == "INC-20260922-0001"


async def test_legacy_db_without_source_ref_gets_the_column(tmp_path) -> None:
    """**已开通的租户库**（`tickets` 表早就在、没有 `source_ref` 列）连上后要能补列。

    不补的话 `CREATE UNIQUE INDEX` 会直接报 "no such column"，而
    `CREATE TABLE IF NOT EXISTS` 对已存在的表什么也不做 —— 这是个只在**升级已有环境**时
    才暴露的坑（本机的 otr / local 正是这种库）。
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE tickets (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, number TEXT,
           title TEXT NOT NULL, service TEXT, namespace TEXT, severity TEXT,
           status TEXT NOT NULL, inputs TEXT NOT NULL, run_ids TEXT NOT NULL,
           created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    c.execute(
        "INSERT INTO tickets VALUES ('old1','otr',NULL,'legacy',NULL,NULL,NULL,'new','{}','[]','t','t')"
    )
    c.commit()
    c.close()

    ts = TicketStore(path)
    await ts.connect()  # 不抛
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(tickets)")}
    assert "source_ref" in cols
    # 历史行（source_ref 为 NULL）不参与唯一约束，也不影响新单
    assert len(await ts.list("otr")) == 1
    await ts.create_once("otr", source_ref="PR-0009", title="new", inputs={})
    assert len(await ts.list("otr")) == 2


async def test_create_from_node_params_maps_bug_report_and_requires_source_ref(tmp_path) -> None:
    """`kind: ticket` 节点的 params → 工单字段；缺 `source_ref` 直接报错。"""
    from agentflow.api.ticket_store import create_from_node_params

    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    out = await create_from_node_params(
        ts,
        "otr",
        {
            "source_ref": "PR-0100",
            "bug_report": {
                "number": "PR-0100",
                "short_description": "结账单打印无反应",
                "severity": "high",
                "cmdb_ci": {"name": "order-service", "namespace": "order"},
            },
            "window_start": "2026-09-21T07:00:00+00:00",
            "window_end": "2026-09-21T08:00:00+00:00",
            "rca": {"hypotheses": ["NPE"]},
            "plan": {"summary": "补空值校验"},
        },
    )
    assert out["created"] is True
    assert out["ticket_number"].startswith("INC-")
    row = await ts.get("otr", out["ticket_id"])
    assert row["title"] == "结账单打印无反应"
    assert row["service"] == "order-service"
    assert row["namespace"] == "order"
    assert row["severity"] == "high"
    assert row["inputs"]["bug_report"]["number"] == "PR-0100"
    assert row["inputs"]["diagnosis"]["plan"]["summary"] == "补空值校验"

    # 缺 source_ref（幂等判据）→ 失败，而不是建一张无法去重的单
    with pytest.raises(ValueError, match="source_ref"):
        await create_from_node_params(ts, "otr", {"bug_report": {"number": "X"}})
