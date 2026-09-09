"""JWT 派生租户（§9.1）+ 租户隔离/配额/审批人（§9.2/§9.3）API 级测试。

与 test_run_api.py 同款装配：httpx ASGITransport + monkeypatch app.service /
app.workflow_store；JWT 模式经 monkeypatch settings.jwt_secret 开启。
"""
from __future__ import annotations

import asyncio
import time

import httpx
import jwt as pyjwt
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app
from agentflow.api.workflow_store import WorkflowStore
from agentflow.config import get_settings
from agentflow.service import RunService
from agentflow.statestore.sqlite import SqliteStateStore
from agentflow.tenants import TenantConfig, TenantRegistry

SECRET = "test-secret"

APPROVAL_YAML = """
name: approval-flow
nodes:
  triage: { agent: triage }
  approve-changes:
    kind: approval
    approvers: [lead-engineer]
    timeout: 3600
  commit:
    agent: committer
    params: { summary: "$.nodes.approve-changes.output.status" }
edges:
  - { from: triage, to: approve-changes }
  - { from: approve-changes, to: commit, when: "$.nodes.approve-changes.output.approved == true" }
"""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _token(tenant: str, sub: str = "alice@company.com", **extra) -> str:
    return pyjwt.encode({"tenant_id": tenant, "sub": sub, **extra}, SECRET, algorithm="HS256")


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    ws = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", ws)
    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    service = RunService(store)
    monkeypatch.setattr(app_mod, "service", service)
    # dev 模式（默认无 secret）——JWT 用例单独开启
    monkeypatch.setattr(get_settings(), "jwt_secret", "", raising=False)
    yield service
    await store.close()


async def _save_workflow(client, yaml_text: str, headers: dict | None = None) -> str:
    resp = await client.post("/workflows", json={"name": "t", "yaml": yaml_text}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _wait_run_status(store, run_id: str, target: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        run = await store.get_run(run_id)
        if run and run["status"] in target:
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} 未达到 {target}，当前 {run and run['status']}")
        await asyncio.sleep(0.02)


# ======================================================================
# JWT 派生（§9.1）
# ======================================================================
async def test_jwt_mode_requires_bearer(svc, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "jwt_secret", SECRET, raising=False)
    auth = {"Authorization": f"Bearer {_token('team-x')}"}
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML, headers=auth)
        resp = await client.post("/run", json={"workflow_id": wid, "inputs": {}})
        assert resp.status_code == 401
        resp = await client.post(
            "/run", json={"workflow_id": wid, "inputs": {}},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert resp.status_code == 401


async def test_jwt_derived_tenant_ignores_client_tenant(svc, monkeypatch) -> None:
    """§9.1 核心断言：tenant 由 claim 派生，body 提交的 tenant_id 被忽略。"""
    monkeypatch.setattr(get_settings(), "jwt_secret", SECRET, raising=False)
    async with _client() as client:
        wid = await _save_workflow(
            client, APPROVAL_YAML, headers={"Authorization": f"Bearer {_token('team-x')}"}
        )
        resp = await client.post(
            "/run",
            json={"workflow_id": wid, "inputs": {}, "tenant_id": "evil-tenant"},
            headers={"Authorization": f"Bearer {_token('team-x')}"},
        )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        # 同租户 token 可读；跨租户 404（不泄漏存在性）；无 token 401
        ok = await client.get(
            f"/runs/{run_id}", headers={"Authorization": f"Bearer {_token('team-x')}"}
        )
        assert ok.status_code == 200
        other = await client.get(
            f"/runs/{run_id}", headers={"Authorization": f"Bearer {_token('team-y')}"}
        )
        assert other.status_code == 404
        anon = await client.get(f"/runs/{run_id}")
        assert anon.status_code == 401


async def test_jwt_expired_and_invalid_and_missing_claim(svc, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "jwt_secret", SECRET, raising=False)
    expired = pyjwt.encode(
        {"tenant_id": "team-x", "exp": 0}, SECRET, algorithm="HS256"
    )
    wrong_key = pyjwt.encode({"tenant_id": "team-x"}, "other-secret", algorithm="HS256")
    no_claim = pyjwt.encode({"sub": "u"}, SECRET, algorithm="HS256")
    async with _client() as client:
        for tok, why in [(expired, "过期"), (wrong_key, "错签名"), (no_claim, "缺 claim")]:
            resp = await client.post(
                "/run",
                json={"inputs": {}},
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert resp.status_code == 401, f"{why} 应 401: {resp.text}"


async def test_dev_mode_explicit_tenant_with_isolation(svc) -> None:
    """dev 模式（无 secret）：显式传参可用，但跨租户读取仍被隔离（§9.2）。"""
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML)
        resp = await client.post(
            "/run", json={"workflow_id": wid, "inputs": {}, "tenant_id": "team-a"}
        )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
        ok = await client.get(f"/runs/{run_id}", headers={"X-Tenant-ID": "team-a"})
        assert ok.status_code == 200
        cross = await client.get(f"/runs/{run_id}")  # 未带租户 → local ≠ team-a
        assert cross.status_code == 404


async def test_audit_tenant_enforced_in_jwt_mode(svc, monkeypatch) -> None:
    """JWT 模式 /audit 的 tenant 由 token 派生，query 参数不能跨租户查询（§9.2）。"""
    monkeypatch.setattr(get_settings(), "jwt_secret", SECRET, raising=False)
    store = svc.store
    await store.append_audit("team-x", tool_name="query_logs", decision="ALLOW",
                             run_id="r1", node_id="n")
    await store.append_audit("team-y", tool_name="run_shell", decision="DENY",
                             run_id="r2", node_id="n")
    async with _client() as client:
        resp = await client.get(
            "/audit?tenant_id=team-y",  # 尝试越权查 team-y
            headers={"Authorization": f"Bearer {_token('team-x')}"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == "team-x"


# ======================================================================
# 配额（§9.3 max_concurrent_runs）
# ======================================================================
async def test_quota_429_when_max_concurrent_reached(svc, monkeypatch) -> None:
    registry = TenantRegistry(
        default=TenantConfig(tenant_id="*", max_concurrent_runs=1), tenants={}
    )
    monkeypatch.setattr(svc, "tenant_registry", registry)
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML)
        r1 = await client.post("/run", json={"workflow_id": wid, "inputs": {}})
        assert r1.status_code == 200
        run_id = r1.json()["run_id"]
        await _wait_run_status(svc.store, run_id, {"waiting_approval"})  # 占用 1 个名额

        r2 = await client.post("/run", json={"workflow_id": wid, "inputs": {}})
        assert r2.status_code == 429
        assert "上限" in r2.json()["detail"]


# ======================================================================
# 审批人白名单（§9.3 approvers）
# ======================================================================
async def test_approver_whitelist_403(svc, monkeypatch) -> None:
    registry = TenantRegistry(
        default=TenantConfig(
            tenant_id="*", approvers={"approve-changes": ["alice@company.com"]}
        ),
        tenants={},
    )
    monkeypatch.setattr(svc, "tenant_registry", registry)
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML)
        run_id = (
            await client.post("/run", json={"workflow_id": wid, "inputs": {}})
        ).json()["run_id"]
        await _wait_run_status(svc.store, run_id, {"waiting_approval"})

        denied = await client.post(
            f"/runs/{run_id}/approve?node_id=approve-changes",
            json={"by": "bob@company.com"},
        )
        assert denied.status_code == 403

        allowed = await client.post(
            f"/runs/{run_id}/approve?node_id=approve-changes",
            json={"by": "alice@company.com"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["run_status"] == "done"
