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
