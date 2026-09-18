"""Worker / 双队列（design §6/§8.6）：run.trigger 执行 + run.command resume/pause/stop。

queue 模式：RunService 只发布消息（status=queued），Worker 消费执行；
审批完成后 API 仅 CAS + 发布 resume 命令，Worker 从 checkpoint 续跑——
全链路零进程内 executor 依赖（多副本安全）。
"""
from __future__ import annotations

import asyncio

from agentflow.core.workflow import Workflow
from agentflow.queue.base import topic_command, topic_trigger
from agentflow.queue.memory import InMemoryQueue
from agentflow.service import RunService
from agentflow.statestore.memory import InMemoryStateStore
from agentflow.worker import Worker

SIMPLE_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage }
  rca:    { agent: root-cause, params: { code: "$.nodes.triage.output.summary" } }
edges:
  - { from: triage, to: rca }
"""

# triage → approve-changes → commit（when approved==true）
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


async def _consume_one(queue: InMemoryQueue, topic: str, handler) -> None:
    async for msg in queue.subscribe(topic):
        await handler(msg)
        break


async def test_worker_executes_trigger_and_run_completes() -> None:
    store, queue = InMemoryStateStore(), InMemoryQueue()
    svc = RunService(store, queue=queue)
    run_id = (await svc.start_run("t1", Workflow.load_yaml(SIMPLE_YAML), {}))["run_id"]
    assert (await store.get_run(run_id))["status"] == "queued"  # 未接单

    worker = Worker(store, queue)
    await _consume_one(queue, topic_trigger("t1"), worker.handle_trigger)
    await worker.wait_run(run_id)

    run = await store.get_run(run_id)
    assert run["status"] == "done"
    nodes = await store.get_nodes(run_id)
    assert nodes["triage"]["status"] == "done"
    assert nodes["rca"]["status"] == "done"


async def test_worker_approve_publishes_resume_and_continues() -> None:
    """§8.6 全链路：trigger → waiting_approval → 审批（仅 CAS+发命令）→ Worker 续跑。"""
    store, queue = InMemoryStateStore(), InMemoryQueue()
    svc = RunService(store, queue=queue)
    worker = Worker(store, queue)
    run_id = (await svc.start_run("t1", Workflow.load_yaml(APPROVAL_YAML), {}))["run_id"]

    await _consume_one(queue, topic_trigger("t1"), worker.handle_trigger)
    await worker.wait_run(run_id)
    assert (await store.get_run(run_id))["status"] == "waiting_approval"

    # 审批：queue 模式只 CAS + 发布 resume（返回 queued，不在 API 进程继续执行）
    res = await svc.approve(run_id, "approve-changes", approved=True, by="lead", comment="ok")
    assert res["run_status"] == "queued"

    await _consume_one(queue, topic_command("t1"), worker.handle_command)
    await worker.wait_run(run_id)
    assert (await store.get_run(run_id))["status"] == "done"
    assert (await store.get_nodes(run_id))["commit"]["status"] == "done"


async def test_worker_pause_then_resume() -> None:
    """pause：当前节点跑完即暂停（checkpoint 保留）；resume 从 checkpoint 继续。"""
    store, queue = InMemoryStateStore(), InMemoryQueue()
    svc = RunService(store, queue=queue)
    release = asyncio.Event()

    async def slow_runner(node, params):
        if node.id == "first":
            await release.wait()
        return {"node": node.id}

    worker = Worker(store, queue, node_runner=slow_runner)
    run_id = (
        await svc.start_run(
            "t1",
            Workflow.load_yaml(
                """
name: slow-flow
nodes:
  first:  { agent: triage }
  second: { agent: root-cause }
edges:
  - { from: first, to: second }
"""
            ),
            {},
        )
    )["run_id"]

    await _consume_one(queue, topic_trigger("t1"), worker.handle_trigger)
    await asyncio.sleep(0.05)  # 让任务进入 first 节点（阻塞在 release）

    await svc.pause_run(run_id)  # 发布 pause 命令
    await _consume_one(queue, topic_command("t1"), worker.handle_command)
    release.set()  # 当前节点放行 → 跑完即暂停
    await worker.wait_run(run_id)
    assert (await store.get_run(run_id))["status"] == "paused"

    # resume：checkpoint 重建（first done / second pending）→ 跑到 done
    await svc.resume_run(run_id, "t1")
    await _consume_one(queue, topic_command("t1"), worker.handle_command)
    await worker.wait_run(run_id)
    assert (await store.get_run(run_id))["status"] == "done"
    assert (await store.get_nodes(run_id))["second"]["status"] == "done"


async def test_worker_stop_marks_cancelled() -> None:
    store, queue = InMemoryStateStore(), InMemoryQueue()
    svc = RunService(store, queue=queue)
    release = asyncio.Event()

    async def blocked_runner(node, params):
        await release.wait()
        return {"node": node.id}

    worker = Worker(store, queue, node_runner=blocked_runner)
    run_id = (await svc.start_run("t1", Workflow.load_yaml(SIMPLE_YAML), {}))["run_id"]
    await _consume_one(queue, topic_trigger("t1"), worker.handle_trigger)
    await asyncio.sleep(0.05)

    await svc.stop_run(run_id)  # 发布 stop 命令
    await _consume_one(queue, topic_command("t1"), worker.handle_command)
    release.set()

    run = await store.get_run(run_id)
    assert run["status"] == "cancelled"
    nodes = await store.get_nodes(run_id)
    assert nodes["triage"]["status"] == "cancelled"


async def test_worker_ignores_trigger_for_terminal_run() -> None:
    """重复 trigger / 已终态 run 不重复执行（幂等接单）。"""
    store, queue = InMemoryStateStore(), InMemoryQueue()
    svc = RunService(store, queue=queue)
    run_id = (await svc.start_run("t1", Workflow.load_yaml(SIMPLE_YAML), {}))["run_id"]

    worker = Worker(store, queue)
    await _consume_one(queue, topic_trigger("t1"), worker.handle_trigger)
    await worker.wait_run(run_id)
    assert (await store.get_run(run_id))["status"] == "done"

    # 重放同一 trigger：run 已终态 → 忽略，不重复执行
    await worker.handle_trigger({"type": "trigger", "run_id": run_id, "tenant_id": "t1"})
    assert run_id not in worker._tasks


# ----------------------------------------------------------------------
# main()：部署形态装配（`--tenant` / `--dsn`）
# ----------------------------------------------------------------------
#
# 这一组锁的是一个**曾经零覆盖**的分支：`main()` 的 `--tenant --dsn`（K8s 容器形态）。
# 缺陷形态：该分支在装配真实 node_runner **之前**就 `return` 了，于是容器形态下
# 每个节点都落到 `DAGExecutor._default_runner`（睡 10ms、返回 `{"ok": True}`）——
# 不调 LLM、不调工具，而 run **照样报 done**。
# 测试全绿是因为既有用例都直接构造 `Worker`，从不走 `main()`。


class _StubManagementStore:
    """main() 只需要它 `await connect()` 得动，不读不写。"""

    def __init__(self) -> None:
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def list_tenants(self, **_kw):
        return []


class _StubPostgresStateStore:
    """替掉 `PostgresStateStore`：本组用例不连真 PG，只验证**装配是否发生**。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def get_run(self, _run_id):  # pragma: no cover —— 本组不跑 run
        return None

    async def cas_update_run_status(self, *_a, **_kw):  # pragma: no cover
        return False


async def _memory_bundle(settings, tenant_id: str):
    """一份形状正确的租户 bundle（Router 用的是同一形状）。

    关键在于它**带着** `mcp` / `agent_config` —— 这正是旧 `--dsn` 分支丢掉的部分：
    只把 StateStore 换掉，MCP 绑定与 agent 配置就没有来源了。
    """
    from agentflow.api.agent_store import build_agent_config_store
    from agentflow.api.mcp_store import build_mcp_store
    from agentflow.api.ticket_store import build_ticket_store
    from agentflow.api.workflow_store import build_workflow_store
    from agentflow.statestore.memory import InMemoryStateStore
    from agentflow.statestore.router import TenantStores

    state = InMemoryStateStore()  # InMemory 无 connect：建好即可用
    stores = {}
    for name, builder in (
        ("workflow", build_workflow_store),
        ("mcp", build_mcp_store),
        ("agent_config", build_agent_config_store),
        ("ticket", build_ticket_store),
    ):
        s = builder(settings)
        await s.connect()
        stores[name] = s
    return TenantStores(tenant_id=tenant_id, state=state, **stores)


async def test_main_dsn_branch_assembles_agent_runner(monkeypatch) -> None:
    """回归：`--tenant --dsn` 必须在 return 前装配真实 node_runner。

    判据不是"日志里说了装"——而是**传给 Worker 的 node_runner 不是 None**：
    None 会被 `DAGExecutor.__init__` 落到 `_default_runner`，全链空转而 run 报 done。
    """
    from agentflow import worker as worker_mod
    from agentflow.agents.runner import AgentNodeRunner
    from agentflow.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")

    captured: dict = {}

    class _RecordingWorker(worker_mod.Worker):
        def __init__(self, *args, **kwargs) -> None:
            captured["node_runner"] = kwargs.get("node_runner")
            captured["tenant_id"] = kwargs.get("tenant_id")
            super().__init__(*args, **kwargs)

        async def run_forever(self) -> None:  # 不真的常驻消费
            captured["ran"] = True

    seen: dict = {}

    async def _fake_build_stores_at_dsn(dsn, tenant_id, _settings, *, state=None):
        seen["dsn"], seen["tenant_id"] = dsn, tenant_id
        return await _memory_bundle(settings, tenant_id)

    monkeypatch.setattr(worker_mod, "Worker", _RecordingWorker)
    monkeypatch.setattr(
        "agentflow.api.management_store.build_management_store",
        lambda _s: _StubManagementStore(),
    )
    monkeypatch.setattr(
        "agentflow.statestore.postgres.PostgresStateStore", _StubPostgresStateStore
    )
    monkeypatch.setattr(
        "agentflow.statestore.router.build_tenant_stores_at_dsn", _fake_build_stores_at_dsn
    )

    await worker_mod.main(
        ["--tenant", "team-alpha", "--dsn", "postgresql://agentflow@db:5432/agentflow"]
    )

    assert captured.get("ran") is True, "worker 未进入消费循环"
    assert captured["tenant_id"] == "team-alpha"
    runner = captured["node_runner"]
    assert runner is not None, (
        "--dsn 分支把 node_runner 传成了 None：容器形态下每个节点都会落到 "
        "_default_runner（{\"ok\": True}），不调 LLM、不调工具，run 仍报 done"
    )
    assert isinstance(runner, AgentNodeRunner)
    # DSN 与租户必须原样透传——bundle 要按**容器可达的那个库**建，而不是 db_ref
    assert seen["dsn"] == "postgresql://agentflow@db:5432/agentflow"
    assert seen["tenant_id"] == "team-alpha"


async def test_main_dsn_branch_without_key_warns_and_falls_back(monkeypatch, caplog) -> None:
    """没配 DeepSeek key → 仍是 mock，但必须**显式告警**（部署漏配是最坏的一种）。"""
    import logging

    from agentflow import worker as worker_mod
    from agentflow.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "deepseek_api_key", "")

    captured: dict = {}

    class _RecordingWorker(worker_mod.Worker):
        def __init__(self, *args, **kwargs) -> None:
            captured["node_runner"] = kwargs.get("node_runner")
            super().__init__(*args, **kwargs)

        async def run_forever(self) -> None:
            captured["ran"] = True

    async def _fake_build_stores_at_dsn(dsn, tenant_id, _settings, *, state=None):
        return await _memory_bundle(settings, tenant_id)

    monkeypatch.setattr(worker_mod, "Worker", _RecordingWorker)
    monkeypatch.setattr(
        "agentflow.api.management_store.build_management_store",
        lambda _s: _StubManagementStore(),
    )
    monkeypatch.setattr(
        "agentflow.statestore.postgres.PostgresStateStore", _StubPostgresStateStore
    )
    monkeypatch.setattr(
        "agentflow.statestore.router.build_tenant_stores_at_dsn", _fake_build_stores_at_dsn
    )

    with caplog.at_level(logging.WARNING, logger="agentflow.worker"):
        await worker_mod.main(["--tenant", "team-alpha", "--dsn", "postgresql://x/y"])

    assert captured["node_runner"] is None  # 回退是设计内行为
    assert captured.get("ran") is True
    assert any("node_runner 回退为 mock" in r.message for r in caplog.records), (
        "mock 回退必须留一条 warning：它不报错、run 照样 done，静默是最坏的"
    )
