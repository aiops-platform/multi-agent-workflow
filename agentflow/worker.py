"""Worker 池（design §6 / §8.6）：消费双队列，执行与命令处理分离于 API 进程。

- ``run.trigger``：加载 run 的原 workflow snapshot（§8.5 版本冻结）+ 节点 checkpoint
  重建 executor → 执行到可释放点（waiting_approval / paused / 终态）→ 释放。
- ``run.command``：
  * ``resume``：审批通过/驳回或手动 resume 后从 checkpoint 继续；
  * ``pause`` ：请求波间暂停（当前节点跑完即返回，checkpoint 已落盘）；
  * ``stop``  ：取消进行中任务，非终态节点置 cancelled。

审批完成后 Worker 零占用（§8.4.3 审批零占用 / §8.6 Worker 释放语义）：审批通过时
API 只发布 resume 命令，Worker 重新拉起 executor。

独立进程部署（queue=kafka）::

    python -m agentflow.worker

进程内形态（queue=memory，run_mode=queue 的本地单进程部署）：API init() 以
``asyncio.create_task(worker.run_forever())`` 拉起，行为与独立进程一致。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .config import get_settings
from .core.dag import TERMINAL
from .executor.dag_executor import DAGExecutor, NodeRunner, WorkflowNodeFailed
from .executor.resume import resume_executor
from .queue import build_queue
from .queue.base import TOPIC_COMMAND, TOPIC_TRIGGER, Queue, topic_command, topic_trigger
from .statestore.base import StateStore
from .statestore.router import TenantStoresRouter, store_resolver

log = logging.getLogger("agentflow.worker")


class Worker:
    def __init__(
        self,
        store: StateStore | Any,
        queue: Queue,
        node_runner: NodeRunner | None = None,
        *,
        tenant_id: str | None = None,
    ) -> None:
        # store 可为普通 StateStore（单库）或 TenantStoresRouter——统一经 resolver
        # 按租户解析（design-v5.3 §5.3：每次操作落到该租户自己的库）
        self._stores = store_resolver(store)
        self.queue = queue
        self.node_runner = node_runner
        # 绑定租户（v5.3 §6.2 生产形态：每租户 Worker 只消费自己的 topic）；
        # None = 消费全局 topic（单租户回退 / 未注册租户 dev 兜底）
        self.tenant_id = tenant_id
        # 本 Worker 正在执行的 run；审批挂起/暂停/终态即移出 → 零占用（§8.6）
        self._tasks: dict[str, asyncio.Task] = {}
        self._executors: dict[str, DAGExecutor] = {}  # run_id → executor（pause 反查用）

    async def _store(self, tenant_id: str | None) -> StateStore:
        return await self._stores.resolve(tenant_id or "local")

    # ------------------------------------------------------------------
    # 常驻消费（trigger 与 command 并行消费）
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        if self.tenant_id is not None:
            trigger_topic, command_topic = (
                topic_trigger(self.tenant_id), topic_command(self.tenant_id),
            )
        else:
            trigger_topic, command_topic = TOPIC_TRIGGER, TOPIC_COMMAND
        await asyncio.gather(
            self._consume(trigger_topic, self.handle_trigger),
            self._consume(command_topic, self.handle_command),
        )

    async def _consume(self, topic: str, handler) -> None:
        async for msg in self.queue.subscribe(topic):
            try:
                await handler(msg)
            except Exception:
                log.exception("[%s] 处理消息失败: %s", topic, msg)

    # ------------------------------------------------------------------
    # run.trigger：加载 snapshot 执行（§8.6 Worker 主循环）
    # ------------------------------------------------------------------
    async def handle_trigger(self, msg: dict) -> None:
        run_id = msg.get("run_id")
        if not run_id:
            log.warning("trigger 缺 run_id: %s", msg)
            return
        tenant_id = msg.get("tenant_id")
        store = await self._store(tenant_id)
        if run_id in self._tasks:
            log.warning("[%s] 已在本 Worker 执行，忽略重复 trigger", run_id)
            return
        run = await store.get_run(run_id)
        if run is None:
            log.warning("[%s] trigger 对应 run 不存在", run_id)
            return
        if run.get("status") in TERMINAL or run.get("status") == "cancelled":
            log.info("[%s] run 已终态（%s），忽略 trigger", run_id, run.get("status"))
            return
        tenant = run["tenant_id"]
        # 接单 CAS（v5.3 §6.3）：queued → running 原子转换，重复消息/多 Worker 恰一个成功
        if not await store.cas_update_run_status(run_id, "queued", "running"):
            log.info("[%s] 接单 CAS 失败（已被其他 Worker 接单），忽略 trigger", run_id)
            return
        ex = await self._build_executor(run_id, tenant, store)
        log.info("[%s] Worker 接单（trigger）", run_id)
        self._executors[run_id] = ex
        self._tasks[run_id] = asyncio.create_task(self._execute(run_id, ex, tenant))

    async def _build_executor(
        self, run_id: str, tenant_id: str, store: StateStore
    ) -> DAGExecutor:
        # trigger 与 resume 统一走 checkpoint 重建：新 run 无 checkpoint → 全 pending，
        # 已有 checkpoint → 续跑（原 snapshot，§8.5）
        return await resume_executor(run_id, tenant_id, store, node_runner=self.node_runner)

    async def _execute(self, run_id: str, ex: DAGExecutor, tenant_id: str) -> None:
        store = await self._store(tenant_id)
        try:
            outcome = await ex.run()
        except WorkflowNodeFailed as exc:
            log.warning("[%s] 执行失败: %s", run_id, exc)
            outcome = "failed"
        except asyncio.CancelledError:
            raise  # stop 由 handle_command 统一置 cancelled
        finally:
            self._tasks.pop(run_id, None)
            self._executors.pop(run_id, None)
        await store.update_run(run_id, status=outcome)
        log.info("[%s] Worker 执行结束 -> %s", run_id, outcome)

    async def wait_run(self, run_id: str) -> None:
        """测试/编排辅助：等待本 Worker 上某 run 的执行任务结束。"""
        task = self._tasks.get(run_id)
        if task is not None:
            await task

    # ------------------------------------------------------------------
    # run.command：resume / pause / stop
    # ------------------------------------------------------------------
    async def handle_command(self, msg: dict) -> None:
        run_id = msg.get("run_id")
        cmd = msg.get("type")
        if not run_id or not cmd:
            log.warning("command 缺 run_id/type: %s", msg)
            return
        if cmd == "resume":
            await self._cmd_resume(msg)
        elif cmd == "pause":
            await self._cmd_pause(run_id)
        elif cmd == "stop":
            await self._cmd_stop(msg)
        else:
            log.warning("[%s] 未知命令: %r", run_id, cmd)

    async def _cmd_resume(self, msg: dict) -> None:
        run_id = msg.get("run_id")
        if run_id in self._tasks:
            log.info("[%s] 正在本 Worker 执行，忽略 resume", run_id)
            return
        store = await self._store(msg.get("tenant_id"))
        run = await store.get_run(run_id)
        if run is None:
            log.warning("[%s] resume 对应 run 不存在", run_id)
            return
        if run.get("status") in TERMINAL or run.get("status") == "cancelled":
            log.info("[%s] run 已终态，忽略 resume", run_id)
            return
        tenant = run["tenant_id"]
        # 接单 CAS：仅 paused / waiting_approval 可被 resume 接单（防双执行）
        claimed = False
        for from_status in ("paused", "waiting_approval"):
            if await store.cas_update_run_status(run_id, from_status, "running"):
                claimed = True
                break
        if not claimed:
            log.info("[%s] 接单 CAS 失败（状态已推进），忽略 resume", run_id)
            return
        ex = await self._build_executor(run_id, tenant, store)
        log.info("[%s] Worker 接单（resume）", run_id)
        self._executors[run_id] = ex
        self._tasks[run_id] = asyncio.create_task(self._execute(run_id, ex, tenant))

    async def _cmd_pause(self, run_id: str) -> None:
        ex = self._executors.get(run_id)
        if ex is None:
            # 不在本 Worker：checkpoint 已在库里，pause 即刻生效（无新节点会被调度）
            log.info("[%s] pause：run 不在本 Worker（已释放），无需处理", run_id)
            return
        ex.request_pause()
        log.info("[%s] pause：已请求，当前节点跑完即暂停", run_id)

    async def _cmd_stop(self, msg: dict) -> None:
        run_id = msg.get("run_id")
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._tasks.pop(run_id, None)
        store = await self._store(msg.get("tenant_id"))
        await _mark_cancelled(store, run_id)
        log.info("[%s] stop：非终态节点已置 cancelled", run_id)


async def _mark_cancelled(store: StateStore, run_id: str) -> None:
    """把非终态节点置 cancelled 并落盘（与 RunService.stop_run 同语义）。"""
    run = await store.get_run(run_id)
    if run is None:
        return
    tenant_id = run["tenant_id"]
    ex = await resume_executor(run_id, tenant_id, store)
    for nid, st in ex.node_states.items():
        if st.get("status") in TERMINAL:
            continue
        ex.node_states[nid] = {"status": "cancelled", "output": None}
        await store.put_node(run_id, tenant_id, nid, ex.node_states[nid])
    await store.update_run(run_id, status="cancelled")


class WorkerPool:
    """按管理库租户清单为每租户起消费循环（v5.3 §6.2 本地/单进程形态）。

    生产（queue=kafka）部署形态是**每租户一个 Worker Deployment**（镜像为该租户
    分支构建，P5）；本池用于 run_mode=queue + memory 的单进程部署：为每个注册租户
    起一个绑定了 ``run.trigger.{tenant}`` / ``run.command.{tenant}`` 的 Worker，
    并保留一个全局 Worker 兜底未注册租户（dev）。租户清单周期性重扫（新租户热接入）。
    """

    def __init__(
        self,
        store: StateStore | Any,
        queue: Queue,
        node_runner: NodeRunner | None = None,
        *,
        tenants_provider: Callable[[], Awaitable[list[str]]] | None = None,
        rescan_interval: float = 30.0,
    ) -> None:
        self._stores = store_resolver(store)
        self.queue = queue
        self.node_runner = node_runner
        self._tenants_provider = tenants_provider
        self._rescan_interval = rescan_interval
        self._consumers: list[asyncio.Task] = []

    async def run_forever(self) -> None:
        # 全局 Worker：兜底未注册租户（dev）+ 兼容旧全局 topic
        base = Worker(self._stores, self.queue, self.node_runner)
        self._consumers.append(asyncio.create_task(base.run_forever()))
        seen: set[str] = set()
        while True:
            if self._tenants_provider is not None:
                try:
                    for tid in await self._tenants_provider():
                        if tid in seen:
                            continue
                        seen.add(tid)
                        w = Worker(self._stores, self.queue, self.node_runner, tenant_id=tid)
                        self._consumers.append(asyncio.create_task(w.run_forever()))
                        log.info("WorkerPool：已接入租户 %s 的消费循环", tid)
                except Exception:
                    log.warning("WorkerPool 租户清单刷新失败", exc_info=True)
            await asyncio.sleep(self._rescan_interval)


async def main(argv: list[str] | None = None) -> None:
    """独立 Worker 进程入口（v5.3 §6.2 多租户形态）。

    装配管理库 + TenantStoresRouter + WorkerPool：
    - ``--tenant <id>``：只消费该租户的 topic（本地调试单租户，无需 provision）；
    - 管理库有 active 租户：WorkerPool 为每租户起消费循环（30s 重扫热接入）；
    - 均无：退化单个全局 Worker（消费旧全局 topic，兼容单库 dev）。
    """
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="agentflow.worker", description="agentflow Worker（多租户）")
    ap.add_argument("--tenant", default=None, help="只消费该租户 topic（本地调试，无需 provision）")
    ap.add_argument("--dsn", default=None,
                    help="postgres:// 直连单库（不装配管理库/Router——K8s 容器内 DB 与 provision 时"
                         "记录的 db_ref 网络不同时用；Worker(单库, tenant_id) 直连共享库）")
    args = ap.parse_args(argv)
    settings = get_settings()
    queue = build_queue(settings)
    node_runner = None
    # v5.3：管理库 + Router（租户 → 租户库）。Worker 未 provision 的租户时，Router 按默认
    # 策略回退（sqlite per-tenant 文件 / postgres 共享 DSN），与 API 侧一致 → 读得到 run。
    from .api.management_store import build_management_store

    mgmt = build_management_store(settings)
    await mgmt.connect()
    router = TenantStoresRouter(settings, mgmt)

    if args.tenant and args.dsn:
        # K8s 容器直连形态：跳过管理库/Router（db_ref 网络不适用容器），单租户 topic + 单库
        from .statestore.postgres import PostgresStateStore

        store = PostgresStateStore(args.dsn)
        await store.connect()
        log.info("Worker(tenant=%s, dsn 直连)：消费 %s / %s",
                 args.tenant, topic_trigger(args.tenant), topic_command(args.tenant))
        await Worker(store, queue, node_runner=node_runner, tenant_id=args.tenant).run_forever()
        return

    # node_runner 装配（需 router 已建：per-tenant MCP store + agent 配置路由）。
    # 此前 Worker 只传 model → 租户 MCP 绑定与 DB agent 配置全部丢失；此处补齐
    # API 侧同款装配。数据源查询全部经 MCP（design-v5.5），无进程内直连。
    if settings.deepseek_api_key:
        from .agents.agent_config import AgentConfigResolver
        from .agents.mcp_manager import MCPClientManager
        from .agents.runner import AgentNodeRunner
        from .agents.scopes import build_model
        from .api.app import build_cmdb

        # agent 配置解析器（租户库 agent_configs 覆盖行）——API 侧的同名 provider 定义在
        # init() 闭包内不可导入，此处用同一 router 自建（Worker 进程不做 CRUD，无需代际缓存）。
        _resolver_cache: dict[str, AgentConfigResolver] = {}

        async def _agent_config_provider(tenant_id: str | None) -> AgentConfigResolver:
            key = tenant_id or "local"
            hit = _resolver_cache.get(key)
            if hit is None:
                bundle = await router.get(key)
                hit = AgentConfigResolver(await bundle.agent_config.list())
                _resolver_cache[key] = hit
            return hit

        async def _mcp_store_provider(tenant_id: str | None):
            bundle = await router.get(tenant_id or "local")
            return bundle.mcp

        mcp_manager = MCPClientManager(router, stores_provider=_mcp_store_provider)

        async def _server_ids_for(agent_name: str, tenant_id: str | None = None):
            """agent → 绑定的 MCP server id 子集（租户库 agent_configs 行）。"""
            resolver = await _agent_config_provider(tenant_id)
            return resolver.server_ids_for(agent_name)

        mcp_manager.server_ids_for = _server_ids_for
        await mcp_manager.load()
        node_runner = AgentNodeRunner(
            build_model(settings),
            mcp_manager=mcp_manager,
            agent_config_provider=_agent_config_provider,
            cmdb=build_cmdb(),
        )
        log.info("node_runner=agent（DeepSeek）数据源经 MCP")
    if args.tenant:
        log.info("Worker(tenant=%s)：消费 %s / %s",
                 args.tenant, topic_trigger(args.tenant), topic_command(args.tenant))
        await Worker(router, queue, node_runner=node_runner, tenant_id=args.tenant).run_forever()
        return
    active = await mgmt.list_tenants(status="active")
    if active:
        async def _active_tenants() -> list[str]:
            return [r["tenant_id"] for r in await mgmt.list_tenants(status="active")]

        log.info("WorkerPool 启动（queue=%s）：为 %d 个 active 租户起消费循环", settings.queue, len(active))
        await WorkerPool(router, queue, node_runner=node_runner,
                         tenants_provider=_active_tenants).run_forever()
        return
    log.warning("管理库暂无 active 租户且未指定 --tenant：退化为全局 topic Worker（单库 dev 形态）")
    await Worker(router, queue, node_runner=node_runner).run_forever()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
