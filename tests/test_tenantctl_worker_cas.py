"""批 B（design-v5.3 §6/§8/§10）：接单 CAS + topic 租户化 + tenantctl + namespace 派生。"""
from __future__ import annotations

import json

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


async def test_provision_seeds_defaults_and_replay_is_idempotent(ctl_env, monkeypatch, capsys) -> None:
    """**provision 完就开箱可用**——三张表都有默认数据，且幂等重放不重复播。

    这是"租户初始化"的端到端证明（sqlite 形态，与真机链路同构：
    provision → 建库 → 建表 → 播种 → 幂等重放）。顺带钉住 CLI 输出里必须看得到计数
    ——只写日志的话，运维确认"到底播没播"就得去翻 API/Worker 的日志。
    """
    from agentflow.agents.agent_config import AgentConfigResolver
    from agentflow.api.agent_store import AgentConfigStore
    from agentflow.api.mcp_store import MCPStore
    from agentflow.api.workflow_store import WorkflowStore
    from agentflow.seed import (
        load_custom_agent_seeds,
        load_dataplane_seed,
        load_workflow_seeds,
    )

    s, tmp_path = ctl_env
    monkeypatch.setattr(s, "seed_defaults", True)  # conftest 默认关（既有测试靠它保持语义）

    assert await tenantctl(["provision", "seed-x"]) == 0
    n_wf = len(load_workflow_seeds())
    n_srv = len(load_dataplane_seed()["servers"])
    # agents 表有两个来源（见 agentflow/seed/agents/）：内置绑定 + 自定义 agent
    n_agt = len(load_dataplane_seed()["bindings"]) + len(load_custom_agent_seeds())
    assert f"workflows={n_wf} servers={n_srv} agents={n_agt}" in capsys.readouterr().out

    db = tmp_path / "data" / "tenants" / "seed-x.db"
    assert db.exists()
    w, m, a = WorkflowStore(db), MCPStore(db), AgentConfigStore(db)
    for store in (w, m, a):
        await store.connect()
    try:
        assert len(await w.list()) == n_wf
        assert len(await m.list()) == n_srv
        assert len(await a.list()) == n_agt
        assert all(r["id"].startswith("seed-") for r in await w.list())
        # 端到端语义：不只是"表里有行"，而是 resolver 真解析得出工具
        resolver = AgentConfigResolver(await a.list())
        assert resolver.server_ids_for("log-analyst")
        assert resolver.server_ids_for("triage") == set()
    finally:
        for store in (w, m, a):
            await store.close()

    # 幂等重放：一条都不该多出来
    assert await tenantctl(["provision", "seed-x"]) == 0
    w2 = WorkflowStore(db)
    await w2.connect()
    try:
        assert len(await w2.list()) == n_wf
    finally:
        await w2.close()


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


# ======================================================================
# 环境体检（_env_preflight）：pg / kafka / 沙箱 三件套
# ======================================================================
class _PreflightSettings:
    """只带体检用到的字段。"""

    def __init__(self, **kw) -> None:
        self.state_store = "postgres"
        self.queue = "kafka"
        self.kafka_bootstrap = "localhost:19092"
        self.sandbox_url = "http://127.0.0.1:44772"
        self.workspace_root = "/Users/someone/agentflow-workspace"
        for k, v in kw.items():
            setattr(self, k, v)


def test_preflight_ok_when_all_three_ready(monkeypatch) -> None:
    """三件套都通 → 空列表（provision 会打 ✓）。"""
    from agentflow import tenantctl

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: _FakeResp(
        {"limits": {"writable_allowlist": ["/workspace", "/tmp", "/Users/someone/agentflow-workspace"]}}
    ))
    assert tenantctl._env_preflight(_PreflightSettings()) == []


def test_preflight_reports_missing_sandbox(monkeypatch) -> None:
    """**没配沙箱必须报出来** —— 这是最容易漏、且症状完全静默的一个。

    不接沙箱时 ws_write_file / ws_run_tests 一律 fail-closed：修复不落盘、测试一条
    不跑，而 tester 只能如实报 passed: false（实测 run_668981c0a7）。
    provision 时不说，就要等第一次 run 才发现。
    """
    from agentflow import tenantctl

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: __import__("contextlib").nullcontext())
    problems = tenantctl._env_preflight(_PreflightSettings(sandbox_url=""))
    assert any("AGENTFLOW_SANDBOX_URL" in p for p in problems), problems


def test_preflight_reports_unreachable_sandbox(monkeypatch) -> None:
    """配了但连不上 → 报"不可达"，并给出起容器的命令。"""
    from agentflow import tenantctl

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: __import__("contextlib").nullcontext())

    def boom(url, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    problems = tenantctl._env_preflight(_PreflightSettings())
    assert any("不可达" in p and "sandbox" in p for p in problems), problems


def test_preflight_reports_workspace_not_writable(monkeypatch) -> None:
    """沙箱在跑、但**可写白名单不含工作区根** → 报出来。

    这种最阴：沙箱是"活着"的（/health 通），而 ws_write_file 会被**正确地**拒掉
    （"路径不在可写白名单"），症状同样是"修复没落盘"。
    """
    from agentflow import tenantctl

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: _FakeResp(
        {"limits": {"writable_allowlist": ["/workspace", "/tmp"]}}
    ))
    problems = tenantctl._env_preflight(_PreflightSettings())
    assert any("可写白名单" in p for p in problems), problems


def test_preflight_reports_bad_kafka_bootstrap(monkeypatch) -> None:
    """kafka 连不上 → 报出来并给出 compose 命令。"""
    from agentflow import tenantctl

    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: _FakeResp(
        {"limits": {"writable_allowlist": ["/Users/someone/agentflow-workspace"]}}
    ))
    problems = tenantctl._env_preflight(_PreflightSettings())
    assert any("Kafka" in p for p in problems), problems


class _FakeResp:
    """urlopen 的最小替身：with 语句 + read()。"""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None
