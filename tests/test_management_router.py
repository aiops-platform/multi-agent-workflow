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
# 新租户默认数据播种（接在 router._build 上——建库即可用）
#
# 这里验的是**接线**（播种真的发生在建库路径上、跨实例幂等、不碰老租户）；
# 播种语义本身（绑定指向真实 id / 不覆盖）在 test_seed_defaults.py。
# ======================================================================
async def test_router_seeds_new_tenant_and_is_idempotent(tmp_path, monkeypatch) -> None:
    """`router.get(新租户)` → 三张表都是种子内容；**换一个 router 实例再来一次** → 数量不变。

    跨实例重跑是关键：真机上同一个新租户常被 API 进程与 Worker 进程各建一次
    （`router.get` 缓存未命中时无并发保护），空表守卫必须在**进程/连接之外**也成立。
    """
    from agentflow.config import get_settings
    from agentflow.seed import load_dataplane_seed, load_workflow_seeds

    s = get_settings()
    monkeypatch.setattr(s, "seed_defaults", True)
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")

    router = TenantStoresRouter(s, None)
    try:
        bundle = await router.get("fresh-a")
        assert len(await bundle.workflow.list()) == len(load_workflow_seeds())
        assert len(await bundle.mcp.list()) == len(load_dataplane_seed()["servers"])
        assert len(await bundle.agent_config.list()) == len(load_dataplane_seed()["bindings"])
    finally:
        await router.aclose()

    # 换实例（= 换进程）再开同一个租户：一条都不该多出来
    router2 = TenantStoresRouter(s, None)
    try:
        bundle2 = await router2.get("fresh-a")
        assert len(await bundle2.workflow.list()) == len(load_workflow_seeds())
        assert len(await bundle2.agent_config.list()) == len(load_dataplane_seed()["bindings"])
    finally:
        await router2.aclose()


async def test_router_seed_respects_switch_and_is_per_tenant(tmp_path, monkeypatch) -> None:
    """开关关掉 → 不播；开着时两个租户**各自**拿到一份。"""
    from agentflow.config import get_settings
    from agentflow.seed import load_workflow_seeds

    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")

    monkeypatch.setattr(s, "seed_defaults", False)
    off = TenantStoresRouter(s, None)
    try:
        assert await (await off.get("no-seed")).workflow.list() == []
    finally:
        await off.aclose()

    monkeypatch.setattr(s, "seed_defaults", True)
    on = TenantStoresRouter(s, None)
    try:
        a, b = await on.get("iso-a"), await on.get("iso-b")
        n = len(load_workflow_seeds())
        assert len(await a.workflow.list()) == n
        assert len(await b.workflow.list()) == n   # 各拿一份，不是共享同一份
        assert (tmp_path / "data" / "tenants" / "iso-a.db").exists()
        assert (tmp_path / "data" / "tenants" / "iso-b.db").exists()
    finally:
        await on.aclose()


async def test_router_seed_leaves_existing_tenant_alone(tmp_path, monkeypatch) -> None:
    """**已有数据的租户不被碰**——"绝不覆盖"在接线层的兜底。

    生产上最危险的场景：给一个**早就存在**的租户（它有自己攒下的 workflow）后来打开
    播种，或重跑 provision / migrate —— 结果把它的 workflow 冲成默认。

    注意场景的构造顺序：必须**先在播种关闭时**把租户建出来并写进自己的数据，再
    **开着播种**重开它。因为播种只在建库那一次起作用——第一次 `router.get` 就会把
    空库播满，之后再想造"已有数据的租户"就来不及了。
    """
    from agentflow.config import get_settings
    from agentflow.seed import load_workflow_seeds

    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")

    # ① 播种关闭时建租户 + 写自己的 workflow（模拟"功能上线前就存在的租户"）
    monkeypatch.setattr(s, "seed_defaults", False)
    old = TenantStoresRouter(s, None)
    try:
        mine = await (await old.get("veteran")).workflow.save("我自己改过的流程", SIMPLE_YAML)
    finally:
        await old.aclose()

    # ② 打开播种，换实例重开（模拟进程重启 / 重跑 provision）
    monkeypatch.setattr(s, "seed_defaults", True)
    new = TenantStoresRouter(s, None)
    try:
        rows = await (await new.get("veteran")).workflow.list()
        assert [r["id"] for r in rows] == [mine], "已有 workflow 的租户被播种碰了"
        assert len(rows) != len(load_workflow_seeds())  # 确认种子一条都没进去
    finally:
        await new.aclose()


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


# ======================================================================
# 每租户独立库（PG）：租户库名派生 + 建/删库的安全护栏
# ======================================================================
def test_pg_default_db_ref_is_per_tenant(monkeypatch) -> None:
    """postgres 下默认 db_ref 必须是**每租户一个库**，不是共享 DSN。

    早期两处 _default_db_ref 的 postgres 分支都直接返回 settings 的共享 DSN，
    于是所有租户落进同一个库；而 workflows/mcp_servers/agent_configs 三张表
    没有 tenant_id 列（设计上靠"每租户一库"物理隔离）→ 跨租户互相可见。
    """
    from agentflow.config import get_settings
    from agentflow.tenants import _default_db_ref

    s = get_settings()
    monkeypatch.setattr(s, "state_store", "postgres")
    monkeypatch.setattr(s, "postgres_dsn", "u:p@h:5432/agentflow")

    a = _default_db_ref("team-a", s)
    b = _default_db_ref("team-b", s)
    assert a["backend"] == "postgres"
    assert a["dsn"].endswith("/agentflow-team-a")
    assert b["dsn"].endswith("/agentflow-team-b")
    assert a["dsn"] != b["dsn"], "两个租户不能指向同一个库"


def test_tenant_db_name_rejects_overlong(monkeypatch) -> None:
    """库名超 PG 的 63 字节上限时报错，而不是截断 —— 截断会让两个租户撞进同一个库。"""
    from agentflow.tenants import tenant_db_name

    assert tenant_db_name("postgresql://u:p@h/db", "team-a") == "db-team-a"
    # 边界：db- + 60 = 63 字节，正好卡在上限，允许
    assert tenant_db_name("postgresql://u:p@h/db", "x" * 60) == "db-" + "x" * 60
    with pytest.raises(ValueError, match="63"):
        tenant_db_name("postgresql://u:p@h/db", "x" * 61)


async def test_ensure_and_drop_refuse_shared_base(monkeypatch) -> None:
    """指向**共享基础库**的 db_ref 一律拒绝：既不为它建库，也不删它。

    旧配置（所有租户共享一个库）下若不设这道闸，deprovision 会把管理库连坐删掉。
    """
    from agentflow.config import get_settings
    from agentflow.tenants import drop_tenant_database, ensure_tenant_database

    s = get_settings()
    monkeypatch.setattr(s, "postgres_dsn", "u:p@h:5432/agentflow")

    shared = {"backend": "postgres", "dsn": "postgresql://u:p@h:5432/agentflow"}
    # 不连库就返回 False —— 说明根本没走到 CREATE/DROP
    assert await ensure_tenant_database(shared, s) is False
    assert await drop_tenant_database(shared, s) is False

    # 非 postgres 直接跳过
    assert await ensure_tenant_database({"backend": "sqlite", "path": "/x"}, s) is False
    assert await drop_tenant_database({"backend": "sqlite", "path": "/x"}, s) is False


async def test_router_fallback_pg_is_per_tenant(monkeypatch) -> None:
    """未注册租户的 PG 回退也走「每租户一库」，与 provision 写入的规则一致。

    这是隔离洞的另一半：router._resolve_ref 曾经自己写一份、返回共享 DSN，
    于是一个**从未开通**的租户 id 也能读到别人的数据。
    """
    from agentflow.config import get_settings
    from agentflow.statestore.router import _default_db_ref as router_default

    s = get_settings()
    monkeypatch.setattr(s, "state_store", "postgres")
    monkeypatch.setattr(s, "postgres_dsn", "u:p@h:5432/agentflow")

    ref = router_default("walk-in", s)
    assert ref["dsn"].endswith("/agentflow-walk-in")

    # memory 是 router 独有的回退档，仍是 memory
    monkeypatch.setattr(s, "state_store", "memory")
    assert router_default("walk-in", s) == {"backend": "memory"}
