"""批 A（design-v5.3 §5）：管理库 + StateStoreRouter + 配置表入租户库。

- ManagementStore：tenants/schema_versions CRUD + db_ref 加密
- TenantStoresRouter：租户库物理隔离（跨租户查不到）+ LRU
- RunService/Sweeper 的租户路由集成
- 审批 default-deny（§4.2）+ 配额锁（§11 第 8 项）
"""
from __future__ import annotations

import asyncio
from datetime import UTC
from pathlib import Path

import pytest
import yaml

from agentflow.api.management_store import (
    ManagementStore,
    decrypt_db_ref,
    encrypt_db_ref,
)
from agentflow.approval.sweeper import ApprovalSweeper
from agentflow.core.workflow import Workflow
from agentflow.lock.memory import InMemoryLock
from agentflow.queue.memory import InMemoryQueue
from agentflow.service import RunService, TenantQuotaExceeded
from agentflow.statestore.router import TenantStoresRouter
from agentflow.tenants import TenantConfig, TenantRegistry, async_bootstrap_tenants

SIMPLE_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage }
edges: []
"""


@pytest.fixture
async def mgmt(tmp_path, monkeypatch):
    from agentflow.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "secret_key", "", raising=False)
    monkeypatch.setattr(s, "jwt_secret", "jwt-test", raising=False)  # 派生加密密钥
    store = ManagementStore(tmp_path / "management.db")
    await store.connect()
    yield store
    await store.close()


# ======================================================================
# ManagementStore
# ======================================================================
async def test_management_store_crud_and_schema_versions(mgmt) -> None:
    await mgmt.upsert_tenant({
        "tenant_id": "team-a", "status": "active", "isolation_level": "strong",
        "namespace": "agentflow-team-a", "workers": 2, "max_concurrent_runs": 5,
        "approvers": {"approve-changes": ["alice"]},
        "db_ref_enc": "plain:{}", "code_branch": "tenant/team-a", "pinned_sha": "abc123",
    })
    row = await mgmt.get_tenant("team-a")
    assert row["isolation_level"] == "strong"
    assert row["approvers"] == {"approve-changes": ["alice"]}
    assert row["pinned_sha"] == "abc123"
    assert [r["tenant_id"] for r in await mgmt.list_tenants(status="active")] == ["team-a"]

    # update（deploy pin）
    assert await mgmt.update_tenant("team-a", pinned_sha="def456", deployed_at="2026-09-09")
    assert (await mgmt.get_tenant("team-a"))["pinned_sha"] == "def456"

    # schema_versions（按 tenant+sha）
    await mgmt.set_schema_version("team-a", "def456", "v7")
    await mgmt.set_schema_version("team-a", "def456", "v8")  # upsert
    assert await mgmt.get_schema_version("team-a", "def456") == "v8"
    assert await mgmt.get_schema_version("team-a", "other") is None


async def test_db_ref_encryption_roundtrip(mgmt, monkeypatch) -> None:
    from agentflow.config import get_settings

    s = get_settings()
    ref = '{"backend": "sqlite", "path": "/tmp/x.db"}'
    enc = encrypt_db_ref(ref, s)  # jwt_secret 派生密钥
    assert enc.startswith("enc:")
    assert "/tmp/x.db" not in enc  # 密文不含明文
    assert decrypt_db_ref(enc, s) == ref

    # 无任何密钥（dev）→ plain 前缀明文，读回一致
    monkeypatch.setattr(s, "jwt_secret", "", raising=False)
    plain = encrypt_db_ref(ref, s)
    assert plain.startswith("plain:")
    assert decrypt_db_ref(plain, s) == ref

    # 密钥不匹配 → 明确报错（不静默错库）
    monkeypatch.setattr(s, "jwt_secret", "another", raising=False)
    with pytest.raises(ValueError, match="密钥不匹配"):
        decrypt_db_ref(enc, s)


async def test_bootstrap_tenants_from_yaml(mgmt, tmp_path, monkeypatch) -> None:
    from agentflow.config import get_settings

    s = get_settings()
    seed = tmp_path / "tenants.yaml"
    seed.write_text(yaml.safe_dump({
        "tenants": {
            "team-a": {"isolation_level": "strong", "max_concurrent_runs": 3,
                       "approvers": {"*": ["boss"]}},
            "team-b": {},
        }
    }), encoding="utf-8")
    monkeypatch.setattr(s, "tenants_file", str(seed))
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")

    assert await async_bootstrap_tenants(mgmt, s) == 2
    assert await async_bootstrap_tenants(mgmt, s) == 0  # 幂等：已存在跳过

    row = await mgmt.get_tenant("team-a")
    assert row["isolation_level"] == "strong"
    assert row["namespace"] == "agentflow-team-a"  # 派生
    assert row["max_concurrent_runs"] == 3
    # db_ref 已加密且可解密回 sqlite 路径
    from agentflow.tenants import parse_db_ref

    ref = parse_db_ref(decrypt_db_ref(row["db_ref_enc"], s))
    assert ref["backend"] == "sqlite"
    assert ref["path"].endswith("tenants/team-a.db")


# ======================================================================
# TenantStoresRouter：租户库物理隔离
# ======================================================================
async def test_router_isolates_tenant_databases(tmp_path, mgmt, monkeypatch) -> None:
    from agentflow.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")
    for tid in ("team-a", "team-b"):
        from agentflow.api.management_store import encrypt_db_ref

        await mgmt.upsert_tenant({
            "tenant_id": tid, "status": "active",
            "namespace": f"agentflow-{tid}",
            "db_ref_enc": encrypt_db_ref(
                f'{{"backend": "sqlite", "path": "{tmp_path}/tenants/{tid}.db"}}', s
            ),
        })

    router = TenantStoresRouter(s, mgmt)
    try:
        bundle_a = await router.get("team-a")
        wf = Workflow.load_yaml(SIMPLE_YAML)
        await bundle_a.state.save_snapshot("team-a", wf.snapshot())
        await bundle_a.state.create_run("run_a1", "team-a", wf.snapshot()["workflow_hash"], {})

        # 租户 B 的库查不到租户 A 的 run（物理隔离，非过滤）
        bundle_b = await router.get("team-b")
        assert await bundle_b.state.get_run("run_a1") is None
        assert await bundle_a.state.get_run("run_a1") is not None
        # 各租户独立 sqlite 文件
        assert Path(f"{tmp_path}/tenants/team-a.db").exists()
        assert Path(f"{tmp_path}/tenants/team-b.db").exists()

        # LRU：容量 1 → 新租户淘汰旧连接并关闭
        small = TenantStoresRouter(s, mgmt, max_size=1)
        try:
            await small.get("team-a")
            await small.get("team-b")  # team-a 被淘汰关闭
            assert "team-a" not in small._cache
        finally:
            await small.aclose()
    finally:
        await router.aclose()


async def test_router_fallback_for_unregistered_tenant(tmp_path, monkeypatch) -> None:
    """未注册租户按默认策略回退（sqlite: data/tenants/{t}.db）——dev 语义。"""
    from agentflow.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")
    router = TenantStoresRouter(s, None)
    bundle = await router.get("walk-in")
    await bundle.state.create_run("run_x", "walk-in", "snap", {})
    assert (await bundle.state.get_run("run_x"))["tenant_id"] == "walk-in"
    assert (tmp_path / "data" / "tenants" / "walk-in.db").exists()
    await router.aclose()


# ======================================================================
# TenantRegistry：from_management + default-deny
# ======================================================================
async def test_registry_from_management_and_default_deny(mgmt) -> None:
    await mgmt.upsert_tenant({
        "tenant_id": "team-a", "status": "active", "namespace": "ns-a",
        "max_concurrent_runs": 3,
        "approvers": {"approve-changes": ["alice"]},  # 只配置了这一个节点
        "db_ref_enc": "plain:{}",
    })
    reg = await TenantRegistry.from_management(mgmt)
    cfg = reg.for_tenant("team-a")
    assert cfg.approvers_for("approve-changes") == ["alice"]
    # §4.2 default-deny：配置过白名单的租户，未命中节点 → 空列表（一律拒绝）
    assert cfg.approvers_for("self-approval-bypass") == []
    assert cfg.effective_namespace == "ns-a"

    # 未配置 approvers 的租户 → 不限制（dev 语义）
    await mgmt.upsert_tenant({
        "tenant_id": "team-b", "status": "active", "namespace": "ns-b",
        "db_ref_enc": "plain:{}",
    })
    reg = await TenantRegistry.from_management(mgmt)  # 重建（CRUD 后刷新）
    assert reg.tenants["team-b"].approvers_for("anything") == []

    # '*' 通配兜底
    cfg2 = TenantConfig(tenant_id="t", approvers={"*": ["anyone"]})
    assert cfg2.approvers_for("whatever-node") == ["anyone"]


# ======================================================================
# RunService + Router 集成 / Sweeper 跨租户 / 配额锁
# ======================================================================
async def test_runservice_routes_to_tenant_database(tmp_path, mgmt, monkeypatch) -> None:
    from agentflow.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")
    router = TenantStoresRouter(s, mgmt)
    svc = RunService(router, tenant_registry=TenantRegistry.builtin())

    wf = Workflow.load_yaml(SIMPLE_YAML)
    run_id = (await svc.start_run("team-a", wf, {}))["run_id"]
    await svc._tasks[run_id]  # 等后台任务结束

    store_a = await router.get("team-a")
    assert (await store_a.state.get_run(run_id))["status"] == "done"
    # 租户 B 库中不存在
    assert await (await router.get("team-b")).state.get_run(run_id) is None
    #审批链路（Router 模式带 tenant_id）
    approval_wf = Workflow.load_yaml("""
name: ap
nodes:
  triage: { agent: triage }
  approve-changes: { kind: approval, approvers: [lead], timeout: 3600 }
edges:
  - { from: triage, to: approve-changes }
""")
    run2 = (await svc.start_run("team-a", approval_wf, {}))["run_id"]
    await svc._tasks[run2]  # 等后台任务执行到 waiting_approval（Worker 释放点）
    res = await svc.approve(run2, "approve-changes", approved=True, by="lead", tenant_id="team-a")
    assert res["run_status"] == "done"
    await router.aclose()


async def test_sweeper_scans_all_tenants(tmp_path, mgmt, monkeypatch) -> None:
    from datetime import datetime, timedelta

    from agentflow.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")
    router = TenantStoresRouter(s, mgmt)
    expired = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    for tid in ("team-a", "team-b"):
        # sweeper 的租户清单来自管理库 → 需注册租户
        await mgmt.upsert_tenant({
            "tenant_id": tid, "status": "active", "namespace": f"agentflow-{tid}",
            "db_ref_enc": encrypt_db_ref(
                f'{{"backend": "sqlite", "path": "{tmp_path}/tenants/{tid}.db"}}', s
            ),
        })
    for tid in ("team-a", "team-b"):
        store = (await router.get(tid)).state
        await store.create_run(f"run_{tid}", tid, "snap", {})
        await store.put_node(f"run_{tid}", tid, "ap", {"status": "waiting_approval"})
        await store.create_approval(
            f"run_{tid}", "ap", tid,
            params={"name": "审批"}, approvers=["lead"], timeout_at=expired,
        )

    queue = InMemoryQueue()
    sweeper = ApprovalSweeper(
        router, queue, interval=1,
        tenants_provider=lambda: _ids(mgmt),
    )
    timed_out = await sweeper.run_once()
    assert {ap["tenant_id"] for ap in timed_out} == {"team-a", "team-b"}
    # 两个租户库的审批都进入终态
    for tid in ("team-a", "team-b"):
        store = (await router.get(tid)).state
        ap = await store.get_approval(f"run_{tid}", "ap")
        assert ap["status"] == "TIMED_OUT"
    await router.aclose()


async def _ids(mgmt) -> list[str]:
    return [r["tenant_id"] for r in await mgmt.list_tenants(status="active")]


async def test_quota_lock_atomicity() -> None:
    """配额 check-then-act 竞态：并发创建下不超过配额（per-tenant 锁临界区）。

    queue 模式下 run 停在 queued（占名额），保证计数确定性。"""
    from agentflow.statestore.memory import InMemoryStateStore

    registry = TenantRegistry(
        default=TenantConfig(tenant_id="*", max_concurrent_runs=2), tenants={}
    )
    svc = RunService(
        InMemoryStateStore(),
        queue=InMemoryQueue(),
        tenant_registry=registry,
        lock=InMemoryLock(),
    )
    wf = Workflow.load_yaml(SIMPLE_YAML)

    results = await asyncio.gather(
        *[svc.create_run("t1", wf, {}) for _ in range(8)], return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, dict)]
    exceeded = [r for r in results if isinstance(r, TenantQuotaExceeded)]
    assert len(ok) == 2  # 恰好配额数成功
    assert len(exceeded) == 6
