"""批 B（design-v5.3 §6/§8/§10）：接单 CAS + topic 租户化 + tenantctl + namespace 派生。"""
from __future__ import annotations

import pytest

from agentflow.api.management_store import ManagementStore
from agentflow.config import get_settings
from agentflow.core.workflow import Workflow
from agentflow.queue.base import topic_trigger
from agentflow.queue.memory import InMemoryQueue
from agentflow.sandbox.action_executor import ActionExecutor, ActionValidationError
from agentflow.service import RunService
from agentflow.statestore.memory import InMemoryStateStore
from agentflow.statestore.sqlite import SqliteStateStore
from agentflow.tenantctl import run_async as tenantctl
from agentflow.tenants import TenantRegistry
from agentflow.worker import Worker, WorkerPool

SIMPLE_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage }
edges: []
"""


# ======================================================================
# 接单 CAS（§6.3）
# ======================================================================
async def test_cas_run_status_two_claimers() -> None:
    store = SqliteStateStore(":memory:")
    await store.connect()
    await store.create_run("run_c", "t", "snap", {})
    await store.update_run("run_c", status="queued")
    # 两个 Worker 恰有一个接单成功
    assert await store.cas_update_run_status("run_c", "queued", "running") is True
    assert await store.cas_update_run_status("run_c", "queued", "running") is False


async def test_worker_double_trigger_executes_once() -> None:
    """重复 trigger / 两个 Worker 同时接单 → runner 只执行一次（接单 CAS）。"""
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    calls = {"n": 0}

    async def runner(node, params):
        calls["n"] += 1
        return {"ok": True}

    wf = Workflow.load_yaml(SIMPLE_YAML)
    svc = RunService(store, queue=queue)
    run_id = (await svc.start_run("t1", wf, {}))["run_id"]
    msg = {"type": "trigger", "run_id": run_id, "tenant_id": "t1"}

    w1 = Worker(store, queue, node_runner=runner)
    w2 = Worker(store, queue, node_runner=runner)
    await w1.handle_trigger(msg)
    await w2.handle_trigger(msg)  # 第二个：CAS 失败（已 running）
    await w1.wait_run(run_id)

    assert calls["n"] == 1
    assert (await store.get_run(run_id))["status"] == "done"


# ======================================================================
# topic-per-tenant（§6.2/P2）
# ======================================================================
async def test_publish_routes_to_tenant_topic() -> None:
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    svc = RunService(store, queue=queue)
    wf = Workflow.load_yaml(SIMPLE_YAML)
    await svc.start_run("team-a", wf, {})
    # 消息落在租户 topic，而非全局 topic
    assert queue._queues.get(topic_trigger("team-a")), "trigger 应落 run.trigger.team-a"
    assert not queue._queues.get("run.trigger"), "不应再发全局 run.trigger"


async def test_tenant_worker_does_not_consume_other_tenant_topic() -> None:
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    calls = {"n": 0}

    async def runner(node, params):
        calls["n"] += 1
        return {"ok": True}

    svc = RunService(store, queue=queue)
    wf = Workflow.load_yaml(SIMPLE_YAML)
    await svc.start_run("team-a", wf, {})

    # 绑定 team-b 的 Worker 不消费 team-a 的 topic
    w_b = Worker(store, queue, node_runner=runner, tenant_id="team-b")
    task = asyncio_create_task_with_timeout(w_b.run_forever())
    await asyncio_wait_short()
    assert calls["n"] == 0
    task.cancel()

    # 绑定 team-a 的 Worker 消费并接单执行
    w_a = Worker(store, queue, node_runner=runner, tenant_id="team-a")
    task_a = asyncio_create_task_with_timeout(w_a.run_forever())
    await asyncio_wait_short()
    await w_a.wait_run(_first_run(store))
    assert calls["n"] == 1
    task_a.cancel()


def asyncio_create_task_with_timeout(coro):
    import asyncio

    return asyncio.create_task(coro)


async def asyncio_wait_short() -> None:
    import asyncio

    await asyncio.sleep(0.05)


def _first_run(store) -> str:
    return next(iter(store._runs))


async def test_workerpool_picks_up_registered_tenants() -> None:
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    calls = {"n": 0}

    async def runner(node, params):
        calls["n"] += 1
        return {"ok": True}

    svc = RunService(store, queue=queue, tenant_registry=TenantRegistry.builtin())
    wf = Workflow.load_yaml(SIMPLE_YAML)
    await svc.start_run("team-z", wf, {})

    pool = WorkerPool(
        store, queue, node_runner=runner,
        tenants_provider=lambda: _ids_async(["team-z"]),
        rescan_interval=0.05,
    )
    task = asyncio_create_task_with_timeout(pool.run_forever())
    try:
        await asyncio_wait_short()
        await asyncio_wait_short()
        run_id = _first_run(store)
        await _wait_status(store, run_id, {"done"})
        assert calls["n"] == 1
    finally:
        task.cancel()


async def _ids_async(ids: list[str]) -> list[str]:
    return ids


async def _wait_status(store, run_id: str, target: set[str]) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + 3.0
    while True:
        run = await store.get_run(run_id)
        if run and run["status"] in target:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} 未达到 {target}，当前 {run and run['status']}")
        await asyncio.sleep(0.02)


# ======================================================================
# tenantctl（§10 幂等 saga）
# ======================================================================
@pytest.fixture
async def ctl_env(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "state_db_path", tmp_path / "data" / "agentflow.db")
    monkeypatch.setattr(s, "secret_key", "", raising=False)
    monkeypatch.setattr(s, "jwt_secret", "jwt-ctl", raising=False)
    return s, tmp_path


async def test_tenantctl_provision_deploy_deprovision(ctl_env) -> None:
    s, tmp_path = ctl_env
    mgmt_path = tmp_path / "data" / "management.db"

    async def open_mgmt():
        m = ManagementStore(mgmt_path)
        await m.connect()
        return m

    # provision（strong + 专属分支）
    assert await tenantctl([
        "provision", "team-x", "--isolation", "strong",
        "--branch", "tenant/team-x", "--quota", "3", "--sha", "abc123def456",
    ]) == 0
    mgmt = await open_mgmt()
    try:
        row = await mgmt.get_tenant("team-x")
        assert row["isolation_level"] == "strong"
        assert row["code_branch"] == "tenant/team-x"
        assert row["pinned_sha"] == "abc123def456"
        assert (await mgmt.get_schema_version("team-x", "abc123def456")) is not None
    finally:
        await mgmt.close()
    # 租户库文件已建（连接触发幂等 DDL）
    assert (tmp_path / "data" / "tenants" / "team-x.db").exists()

    # deploy：pin 新 SHA
    assert await tenantctl(["deploy", "team-x", "--sha", "aa11bb22cc33"]) == 0
    mgmt = await open_mgmt()
    try:
        row = await mgmt.get_tenant("team-x")
        assert row["pinned_sha"] == "aa11bb22cc33"
        assert row["image_tag"] == "team-x-aa11bb22cc33"
        assert row["deployed_at"] is not None
    finally:
        await mgmt.close()

    # 治理规则 §9.2(4)：standard 租户不允许专属分支
    assert await tenantctl(["provision", "team-y", "--branch", "tenant/team-y"]) == 2

    # 幂等重放
    assert await tenantctl(["provision", "team-x"]) == 0

    # deprovision（含数据删除）
    assert await tenantctl(["deprovision", "team-x", "--confirm-delete"]) == 0
    assert not (tmp_path / "data" / "tenants" / "team-x.db").exists()
    mgmt = await open_mgmt()
    try:
        assert (await mgmt.get_tenant("team-x"))["status"] == "deleted"
    finally:
        await mgmt.close()


async def test_tenantctl_migrate_fans_out(ctl_env) -> None:
    s, tmp_path = ctl_env
    assert await tenantctl(["provision", "team-a"]) == 0
    assert await tenantctl(["provision", "team-b"]) == 0
    assert await tenantctl(["migrate"]) == 0  # 扇出全部 active 租户
    mgmt = ManagementStore(tmp_path / "data" / "management.db")
    await mgmt.connect()
    for tid in ("team-a", "team-b"):
        assert (await mgmt.get_schema_version(tid, "init")) is not None
    await mgmt.close()


# ======================================================================
# namespace 派生（§8 P3）
# ======================================================================
async def test_action_executor_tenant_namespace_boundary(monkeypatch) -> None:
    """租户动作 namespace 越界即拒（校验先于 K8s API 调用）。"""
    from kubernetes import config as k8s_config

    monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(k8s_config, "load_kube_config", lambda: None)
    ex = ActionExecutor(tenant_namespace_fn=lambda t: f"agentflow-{t}")

    # 越界：team-a 只能操作 agentflow-team-a
    with pytest.raises(ActionValidationError, match="越出"):
        await ex.execute(
            "scale_deployment", namespace="other-ns", tenant_id="team-a",
            name="x", replicas=1,
        )
