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
from .lock import (
    LEASE_REFRESH_SEC,
    LEASE_TTL_SEC,
    build_lock,
    run_exec_lease_key,
)
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
        lock=None,
    ) -> None:
        # store 可为普通 StateStore（单库）或 TenantStoresRouter——统一经 resolver
        # 按租户解析（design-v5.3 §5.3：每次操作落到该租户自己的库）
        self._stores = store_resolver(store)
        self.queue = queue
        self.node_runner = node_runner
        # 绑定租户（v5.3 §6.2 生产形态：每租户 Worker 只消费自己的 topic）；
        # None = 消费全局 topic（单租户回退 / 未注册租户 dev 兜底）
        self.tenant_id = tenant_id
        # 执行租约用（见 `lock/__init__.py` 的说明）。**None = 不持租约**：
        # 单测与未接线 redis 的部署走这条，此时"有没有执行者"是**未知**而不是"没有"
        # （见 `api/app.py` 的 `executor_alive` 三态）—— 判不出就不许强制暂停。
        self.lock = lock
        # 本 Worker 正在执行的 run；审批挂起/暂停/终态即移出 → 零占用（§8.6）
        self._tasks: dict[str, asyncio.Task] = {}
        self._executors: dict[str, DAGExecutor] = {}  # run_id → executor（pause 反查用）
        self._lease_tasks: dict[str, asyncio.Task] = {}  # run_id → 续期任务

    async def _store(self, tenant_id: str | None) -> StateStore:
        return await self._stores.resolve(tenant_id or "local")

    # ------------------------------------------------------------------
    # 执行租约：「这条 run 有执行者」—— 键名与 TTL 见 lock/__init__.py
    # ------------------------------------------------------------------
    async def _claim_lease(self, run_id: str) -> None:
        """接单后取租约并起续期任务。

        **取不到不阻断执行** —— redis 挂了不该让整个平台停摆。代价是那条 run 的
        "有没有执行者"变成**未知**，而未知**不允许**被当成"没有"：暂停分流只在
        明确无租约时才强制（见 `RunService.pause_run`）。
        """
        if self.lock is None:
            return
        key = run_exec_lease_key(run_id)
        try:
            ok = await self.lock.acquire(key, ttl=LEASE_TTL_SEC)
        except Exception as exc:  # noqa: BLE001 - 租约是增强，不是执行前提
            log.warning("[%s] 取执行租约失败（%s）—— 该 run 的存活状态将不可判定", run_id, exc)
            return
        if not ok:
            log.warning("[%s] 执行租约已被占用 —— 仍继续执行，但存活状态不可判定", run_id)
            return
        self._lease_tasks[run_id] = asyncio.create_task(self._renew_lease(run_id))

    async def _renew_lease(self, run_id: str) -> None:
        """定期续期，直到续不动或任务被取消。

        续不动意味着**本 Worker 已被认为不再执行这条 run**（TTL 到期后有人接管，
        或锁易主）。此时不自动中止执行 —— 那需要 fencing（把租约代际传给下游写操作），
        属后续工作；但必须**大声说出来**，因为它意味着可能有两个执行体。
        """
        key = run_exec_lease_key(run_id)
        while True:
            await asyncio.sleep(LEASE_REFRESH_SEC)
            try:
                if not await self.lock.refresh(key, ttl=LEASE_TTL_SEC):
                    log.error(
                        "[%s] 执行租约续期失败 —— 该 run 可能已被判定为无执行者并被接管。"
                        "本 Worker 仍在执行，两条执行体会写同一份 checkpoint",
                        run_id,
                    )
                    return
            except Exception as exc:  # noqa: BLE001 - 抖动不该直接放弃租约
                log.warning("[%s] 续期异常（%s），下一轮重试", run_id, exc)

    async def _release_lease(self, run_id: str) -> None:
        task = self._lease_tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
        if self.lock is None:
            return
        try:
            await self.lock.release(run_exec_lease_key(run_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 释放执行租约失败（%s）—— TTL 到期后自动消失", run_id, exc)

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
        # `failed` 不在 TERMINAL（见 core/dag.py 的说明）—— 但**失败的 run 同样不该被
        # 重新 trigger/resume**，所以这里显式带上，和 cancelled 一样。
        if run.get("status") in TERMINAL or run.get("status") in ("cancelled", "failed"):
            log.info("[%s] run 已终态（%s），忽略 trigger", run_id, run.get("status"))
            return
        tenant = run["tenant_id"]
        # 接单 CAS（v5.3 §6.3）：queued → running 原子转换，重复消息/多 Worker 恰一个成功
        if not await store.cas_update_run_status(run_id, "queued", "running"):
            log.info("[%s] 接单 CAS 失败（已被其他 Worker 接单），忽略 trigger", run_id)
            return
        ex = await self._build_executor(run_id, tenant, store)
        log.info("[%s] Worker 接单（trigger）", run_id)
        await self._claim_lease(run_id)
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
            # 租约**先于**状态落库释放：释放它 = "这条 run 不再有执行者"，
            # 而紧随其后的 `update_run` 才把 run 推进到释放点（paused/终态）。
            # 顺序反过来的话，中间那一瞬会出现"状态还是 running、但已无执行者"，
            # 正是僵尸的指纹 —— 虽然极短，但没必要制造。
            await self._release_lease(run_id)
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
        # `failed` 不在 TERMINAL（见 core/dag.py 的说明）—— 但**失败的 run 同样不该被
        # 重新 trigger/resume**，所以这里显式带上，和 cancelled 一样。
        if run.get("status") in TERMINAL or run.get("status") in ("cancelled", "failed"):
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
        await self._claim_lease(run_id)
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
        lock=None,
    ) -> None:
        self._stores = store_resolver(store)
        self.queue = queue
        self.node_runner = node_runner
        self._tenants_provider = tenants_provider
        self._rescan_interval = rescan_interval
        # 执行租约：池里每个 Worker 共用同一个 Lock 适配器（各自 token 不同，互不干扰）
        self.lock = lock
        self._consumers: list[asyncio.Task] = []

    async def run_forever(self) -> None:
        # 全局 Worker：兜底未注册租户（dev）+ 兼容旧全局 topic
        base = Worker(self._stores, self.queue, self.node_runner, lock=self.lock)
        self._consumers.append(asyncio.create_task(base.run_forever()))
        seen: set[str] = set()
        while True:
            if self._tenants_provider is not None:
                try:
                    for tid in await self._tenants_provider():
                        if tid in seen:
                            continue
                        seen.add(tid)
                        w = Worker(self._stores, self.queue, self.node_runner,
                                   tenant_id=tid, lock=self.lock)
                        self._consumers.append(asyncio.create_task(w.run_forever()))
                        log.info("WorkerPool：已接入租户 %s 的消费循环", tid)
                except Exception:
                    log.warning("WorkerPool 租户清单刷新失败", exc_info=True)
            await asyncio.sleep(self._rescan_interval)


async def build_node_runner(settings, stores: Any) -> NodeRunner | None:
    """装配真实 node_runner（DeepSeek + per-tenant MCP + DB agent 配置）。

    ``stores``：满足 ``async get(tenant_id) -> TenantStores`` 契约的对象——
    Router 形态传 ``TenantStoresRouter``，``--dsn`` 直连形态传
    ``_FixedStores``（同一个契约，两种取库方式）。

    **两条路径必须都走到这里**：``--dsn`` 分支曾在这段装配**之前** `return`，
    于是容器形态下每个节点都落到 ``_default_runner``（睡 10ms、返回
    ``{"ok": True}``）——不调 LLM、不调工具，run 照样报 `done`。
    未配置 DeepSeek key 时返回 None（mock runner 回退，本地/CI 语义）。
    """
    if not settings.deepseek_api_key:
        # 显式告警：这条回退**不报错、run 照样 done**，只留一行日志——部署形态漏配
        # 这个变量时，整条链会"跑得很成功"地空转（每个节点 `{"ok": True}`）。
        log.warning(
            "未配置 DeepSeek key（AGENTFLOW_DEEPSEEK_API_KEY / DEEPSEEK_API_KEY）"
            "→ node_runner 回退为 mock：节点不调 LLM、不调工具，run 仍报 done"
        )
        return None

    from .agents.config_sync import TenantConfigSync
    from .agents.mcp_manager import MCPClientManager
    from .agents.runner import AgentNodeRunner
    from .agents.scopes import build_model
    from .sandbox import build_sandbox_client

    async def _mcp_store_provider(tenant_id: str | None):
        bundle = await stores.get(tenant_id or "local")
        return bundle.mcp

    mcp_manager = MCPClientManager(stores, stores_provider=_mcp_store_provider)

    # 配置热载：Worker 是独立进程，看不到 API 侧的内存代际计数器，只能按**库内指纹**
    # 判定配置是否变过（见 agents/config_sync.py）。此前这里是永久缓存 → 绑定新 MCP
    # server 后必须重启 Worker 才生效。
    sync = TenantConfigSync(stores, mcp_manager, interval=settings.config_refresh_sec)

    async def _agent_config_provider(tenant_id: str | None):
        """agent 配置解析器（租户库 agent_configs 覆盖行），按指纹自动热载。"""
        return await sync.resolver(tenant_id)

    async def _server_ids_for(agent_name: str, tenant_id: str | None = None):
        """agent → 绑定的 MCP server id 子集（租户库 agent_configs 行）。"""
        resolver = await _agent_config_provider(tenant_id)
        return resolver.server_ids_for(agent_name)

    mcp_manager.server_ids_for = _server_ids_for
    await mcp_manager.load()
    sandbox = build_sandbox_client(settings)
    if sandbox is None:
        # 不接线沙箱属于**部署配置缺失**：写文件/跑测试会以明确的工具错误失败
        # （不会回退到本地执行）。这里提前告警，别等到 fix 节点跑到一半才发现。
        log.warning(
            "未配置 AGENTFLOW_SANDBOX_URL → 工作区写文件与测试命令不可用"
            "（调用即报错，不会回退到 worker 本地执行）"
        )
    runner = AgentNodeRunner(
        build_model(settings),
        mcp_manager=mcp_manager,
        agent_config_provider=_agent_config_provider,
        sandbox_client=sandbox,
    )
    log.info("node_runner=agent（DeepSeek）数据源经 MCP%s",
             "，写/测试经沙箱" if sandbox is not None else "")
    return runner


class _FixedStores:
    """把一个固定 bundle 适配成 Router 的 ``.get()`` 契约（``--dsn`` 直连形态）。

    为什么不能把 ``--dsn`` 分支的 StateStore 塞给装配：装配要的是
    ``workflows`` / ``mcp_servers`` / ``agent_configs`` **三张表**所在的那个库
    （见 ``statestore/router.build_tenant_stores_at_dsn``），只给 StateStore
    会让 MCP 绑定与 agent 配置一起消失。
    """

    def __init__(self, bundle: Any) -> None:
        self._bundle = bundle

    async def get(self, tenant_id: str) -> Any:  # noqa: ARG002 —— 单租户形态，恒返回同一 bundle
        return self._bundle


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
                         "记录的 db_ref 网络不同时用；Worker(单库, tenant_id) 直连共享库。"
                         "该 DSN 即 MCP 绑定与 agent 配置所在的那个库，node_runner 照常装配）")
    args = ap.parse_args(argv)
    settings = get_settings()
    queue = build_queue(settings)
    # v5.3：管理库 + Router（租户 → 租户库）。Worker 未 provision 的租户时，Router 按默认
    # 策略回退（sqlite per-tenant 文件 / postgres 共享 DSN），与 API 侧一致 → 读得到 run。
    from .api.management_store import build_management_store

    mgmt = build_management_store(settings)
    await mgmt.connect()
    router = TenantStoresRouter(settings, mgmt)

    if args.tenant and args.dsn:
        # K8s 容器直连形态：不装配管理库/Router（db_ref 网络不适用容器），单租户 topic + 单库。
        # ⚠️ 但**只换 StateStore 会让装配丢失**：bundle 里还有这个库的 mcp_servers /
        # agent_configs（MCP 绑定与 agent 配置），所以按同一个 DSN 把整套 bundle 建出来。
        from .statestore.postgres import PostgresStateStore
        from .statestore.router import build_tenant_stores_at_dsn

        store = PostgresStateStore(args.dsn)
        await store.connect()
        bundle = await build_tenant_stores_at_dsn(
            args.dsn, args.tenant, settings, state=store
        )
        node_runner = await build_node_runner(settings, _FixedStores(bundle))
        log.info("Worker(tenant=%s, dsn 直连)：消费 %s / %s",
                 args.tenant, topic_trigger(args.tenant), topic_command(args.tenant))
        await Worker(store, queue, node_runner=node_runner, tenant_id=args.tenant,
                     lock=build_lock(settings)).run_forever()
        return

    # node_runner 装配（需 router 已建：per-tenant MCP store + agent 配置路由）。
    # 此前 Worker 只传 model → 租户 MCP 绑定与 DB agent 配置全部丢失；此处补齐
    # API 侧同款装配。数据源查询全部经 MCP（design-v5.6），无进程内直连。
    node_runner = await build_node_runner(settings, router)
    if args.tenant:
        log.info("Worker(tenant=%s)：消费 %s / %s",
                 args.tenant, topic_trigger(args.tenant), topic_command(args.tenant))
        await Worker(router, queue, node_runner=node_runner, tenant_id=args.tenant,
                     lock=build_lock(settings)).run_forever()
        return
    active = await mgmt.list_tenants(status="active")
    if active:
        async def _active_tenants() -> list[str]:
            return [r["tenant_id"] for r in await mgmt.list_tenants(status="active")]

        log.info("WorkerPool 启动（queue=%s）：为 %d 个 active 租户起消费循环", settings.queue, len(active))
        await WorkerPool(router, queue, node_runner=node_runner,
                         tenants_provider=_active_tenants,
                         lock=build_lock(settings)).run_forever()
        return
    log.warning("管理库暂无 active 租户且未指定 --tenant：退化为全局 topic Worker（单库 dev 形态）")
    await Worker(router, queue, node_runner=node_runner,
                 lock=build_lock(settings)).run_forever()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
