"""B1：``GET /runs`` 列表 + ``GET /runs/{id}`` 补字段（inputs / 时间戳 / 节点 error·耗时·重试）。

与 test_run_api.py 同款夹具：httpx.AsyncClient + ASGITransport，monkeypatch 掉
app.workflow_store 与 app.service，避免污染 data/agentflow.db。
"""
import asyncio
import time

import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app
from agentflow.api.workflow_store import WorkflowStore
from agentflow.service import RunService
from agentflow.statestore.sqlite import SqliteStateStore

VALID_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug_report" } }
  rca:    { agent: root-cause, params: { code: "$.nodes.triage.output.summary" } }
edges:
  - { from: triage, to: rca }
"""

FAILING_YAML = """
name: failing-flow
nodes:
  triage: { agent: triage }
  rca:    { agent: root-cause }
edges:
  - { from: triage, to: rca }
"""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def _build_service(tmp_path, monkeypatch, node_runner=None) -> RunService:
    ws = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", ws)
    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    service = RunService(store, node_runner=node_runner)
    monkeypatch.setattr(app_mod, "service", service)
    return service


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    return await _build_service(tmp_path, monkeypatch)


async def _save_workflow(client, yaml_text: str, name: str = "test-flow") -> str:
    resp = await client.post("/workflows", json={"name": name, "yaml": yaml_text})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _wait_status(client, run_id: str, target: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        data = (await client.get(f"/runs/{run_id}")).json()
        if data["status"] in target:
            return data
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} 未达 {target}，当前 {data['status']}")
        await asyncio.sleep(0.03)


# ---- GET /runs 列表 ----


async def test_list_runs_includes_created_run(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML, "simple-flow")
        run_id = (await client.post(
            "/run", json={"workflow_id": wid, "ticket": {"bug_report": {"title": "结账无响应"}}}
        )).json()["run_id"]
        await _wait_status(client, run_id, {"success"})

        rows = (await client.get("/runs")).json()

    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run_id
    assert row["workflow"] == "simple-flow"
    assert row["status"] == "success"
    # 列表也回显 inputs —— 否则列表页显示不出「这条 run 是哪个 ticket」
    assert row["inputs"]["bug_report"]["title"] == "结账无响应"
    assert row["created_at"] and row["updated_at"]
    assert row["total_tokens"] == 0 and row["total_cost"] == 0.0  # mock 无 LLM，诚实为 0


async def test_list_runs_ordering_is_deterministic(svc) -> None:
    """排序：created_at DESC, run_id DESC。

    **注意 created_at 只到秒**（sqlite 的 ``CURRENT_TIMESTAMP`` 是 ``YYYY-MM-DD HH:MM:SS``），
    所以同一秒内创建的 run **无法按创建先后排序** —— 次级键是 run_id（哈希，非时间序）。
    这里断言的是「集合完整 + 排序稳定」，不断言同秒内的创建顺序（那不可保证）。
    真实使用中 run 之间通常相隔数秒以上，不受影响。
    """
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML, "simple-flow")
        ids = []
        for i in range(3):
            ids.append((await client.post(
                "/run", json={"workflow_id": wid, "ticket": {"bug_report": {"n": i}}}
            )).json()["run_id"])

        first = (await client.get("/runs")).json()
        second = (await client.get("/runs")).json()

    assert {r["run_id"] for r in first} == set(ids)
    # 稳定：同样数据两次调用顺序一致
    assert [r["run_id"] for r in first] == [r["run_id"] for r in second]
    # 主键降序：created_at 单调不增
    stamps = [r["created_at"] for r in first]
    assert stamps == sorted(stamps, reverse=True)


async def test_list_runs_status_filter(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML, "simple-flow")
        run_id = (await client.post("/run", json={"workflow_id": wid, "ticket": {}})).json()["run_id"]
        await _wait_status(client, run_id, {"success"})

        assert len((await client.get("/runs?status=success")).json()) == 1
        assert (await client.get("/runs?status=running")).json() == []


async def test_list_runs_tenant_isolation(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML, "simple-flow")
        await client.post("/run", json={"workflow_id": wid, "ticket": {}})

        # dev 模式：X-Tenant-ID 头决定租户
        other = await client.get("/runs", headers={"X-Tenant-ID": "other-tenant"})

    assert other.json() == []


# ---- GET /runs/{id} 补字段 ----


async def test_get_run_echoes_inputs_and_timestamps(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML, "simple-flow")
        ticket = {
            "bug_report": {"number": "INC0012345", "short_description": "订单服务结账无响应"},
            "window_start": "2026-08-19T14:00:00+08:00",
            "window_end": "2026-08-19T15:00:00+08:00",
        }
        run_id = (await client.post("/run", json={"workflow_id": wid, "ticket": ticket})).json()["run_id"]
        run = await _wait_status(client, run_id, {"success"})

    # v5.5 §7.1 要求时间窗由调用方下发 —— 但此前查不回来，连「诊断了哪段时间」都看不到
    assert run["inputs"]["window_start"] == "2026-08-19T14:00:00+08:00"
    assert run["inputs"]["bug_report"]["number"] == "INC0012345"
    assert run["created_at"] and run["updated_at"]


async def test_get_run_node_has_timing_and_attempts(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML, "simple-flow")
        run_id = (await client.post("/run", json={"workflow_id": wid, "ticket": {}})).json()["run_id"]
        run = await _wait_status(client, run_id, {"success"})

    node = run["nodes"]["triage"]
    assert node["started_at"] and node["ended_at"]
    assert isinstance(node["duration_ms"], int) and node["duration_ms"] >= 0
    # attempts 是**尝试总次数**（含首次），不是重试次数 —— 一次过即 1。
    # 前端显示「重试 N 次」时应算 max(0, attempts - 1)。
    assert node["attempts"] == 1
    assert node["error"] is None


async def test_get_run_node_error_surfaces(tmp_path, monkeypatch) -> None:
    """节点失败时 cp.error 必须透出 —— 此前 API 不映射，前端看不到失败原因。"""

    async def failing_runner(node, params):
        raise RuntimeError("boom")

    await _build_service(tmp_path, monkeypatch, node_runner=failing_runner)

    async with _client() as client:
        wid = await _save_workflow(client, FAILING_YAML, "failing-flow")
        run_id = (await client.post("/run", json={"workflow_id": wid, "ticket": {}})).json()["run_id"]
        run = await _wait_status(client, run_id, {"failed"})

    node = run["nodes"]["triage"]
    assert node["status"] == "failed"
    assert "boom" in (node["error"] or "")
    # 失败路径也要有耗时（改造前失败节点的 state 是重新构造的，时间戳会丢）
    assert node["started_at"] and node["ended_at"]
    assert isinstance(node["duration_ms"], int)
