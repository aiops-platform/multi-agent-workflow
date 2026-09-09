"""RunService：create / start / resume / approve / pause / stop 的编排入口。

两种执行模式（§6 / §8.6，配置 ``AGENTFLOW_RUN_MODE``）：

- **inline**（默认，本地 MVP）：进程内直接执行（Worker 即调用方）。
- **queue**：API 只负责「冻结 snapshot → 建 run → 发布 run.trigger」，执行由
  Worker 消费（``agentflow.worker.Worker``）；approve/resume/pause/stop 发布
  ``run.command``。审批等待期间 API 与 Worker 零占用，多副本安全（executor
  状态全部来自 checkpoint，不再依赖进程内 ``_executors``）。

多租户（design-v5.3）：

- **存储路由**：``store`` 参数可为普通 StateStore（单库，既有用法/测试零改动）或
  :class:`~agentflow.statestore.router.TenantStoresRouter`（P4 租户库路由）——
  统一经 ``store_resolver`` 解析，每次操作落到该租户自己的库。
- **配额原子化**：``lock``（``agentflow/lock`` 接口）以 per-tenant 锁包住
  「配额检查 + run 占位」临界区，堵 check-then-act 竞态（§11 第 8 项）。
- **租户配置**：``tenant_registry`` 提供配额（max_concurrent_runs → 429）与
  审批人 default-deny 白名单（→ 403）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from .config import get_settings
from .core.dag import TERMINAL
from .core.workflow import Workflow
from .executor.dag_executor import DAGExecutor, NodeRunner, WorkflowNodeFailed
from .executor.resume import resume_executor
from .queue.base import Queue, topic_command, topic_trigger
from .statestore.base import StateStore
from .statestore.router import store_resolver
from .tenants import TenantRegistry

log = logging.getLogger("agentflow.service")


class TenantQuotaExceeded(Exception):
    """租户并发 run 数超配额（§9.3 max_concurrent_runs）→ API 映射 429。"""


class ApproverNotAllowed(Exception):
    """审批人不在租户白名单（§9.3 approvers，default-deny）→ API 映射 403。"""


class InputsValidationError(ValueError):
    """run 入参不合法（v5.3 §7.3：加固姿态下 inputs.repos 直传被封堵）→ API 映射 400。"""


class RunService:
    def __init__(
        self,
        store: StateStore | Any,
        node_runner: NodeRunner | None = None,
        *,
        queue: Queue | None = None,
        tenant_registry: TenantRegistry | None = None,
        lock=None,
    ) -> None:
        self._stores = store_resolver(store)  # 单库固定解析 / Router 按租户解析
        # 兼容既有用法/测试的单库访问（Router 模式为 None，请用 store_for(tenant_id)）
        self.store = store if hasattr(store, "get_run") else None
        self.node_runner = node_runner
        # queue 非空 = queue 模式（发布 trigger/command，不进程内执行）
        self.queue = queue
        self.tenant_registry = tenant_registry
        self.lock = lock  # per-tenant 配额临界区（None = 不加锁，单进程测试）
        self._executors: dict[str, DAGExecutor] = {}  # 仅 inline 模式使用
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 租户校验（§9.3 / v5.3 §4.2）
    # ------------------------------------------------------------------
    async def store_for(self, tenant_id: str) -> StateStore:
        """按租户解析其数据面库（API 端点用；单库模式忽略租户恒返回同一库）。"""
        return await self._stores.resolve(tenant_id)

    async def _check_quota(self, tenant_id: str, store: StateStore) -> None:
        if self.tenant_registry is None:
            return
        quota = self.tenant_registry.for_tenant(tenant_id)
        active = await store.count_active_runs(tenant_id)
        if active >= quota.max_concurrent_runs:
            raise TenantQuotaExceeded(
                f"租户 {tenant_id} 并发 run 数已达上限（{active}/{quota.max_concurrent_runs}）"
            )

    def _check_approver(self, tenant_id: str, node_id: str, by: str) -> None:
        if self.tenant_registry is None:
            return
        cfg = self.tenant_registry.for_tenant(tenant_id)
        if not cfg.approvers:
            return  # 未配置审批人 → 不限制（dev 语义）
        # §4.2 default-deny：租户配置过白名单后，未命中节点（无节点级配置也无 "*"）
        # → approvers_for 返回 [] → 一律拒绝（防自建 workflow 换审批节点 id 绕过）
        allowed = cfg.approvers_for(node_id)
        if by not in allowed:
            raise ApproverNotAllowed(
                f"{by!r} 不在租户 {tenant_id} 对审批节点 {node_id!r} 的审批人名单内"
            )

    async def _acquire_tenant_lock(self, tenant_id: str, timeout: float = 10.0) -> str:
        """获取 per-tenant 配额临界区锁（非阻塞 acquire + 轮询等待）。"""
        key = f"tenant-quota:{tenant_id}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await self.lock.acquire(key, ttl=timeout + 5.0):
            if loop.time() > deadline:
                raise TimeoutError(f"获取租户 {tenant_id} 配额锁超时")
            await asyncio.sleep(0.02)
        return key

    # ------------------------------------------------------------------
    # 创建/触发
    # ------------------------------------------------------------------
    async def create_run(
        self,
        tenant_id: str,
        workflow: Workflow,
        inputs: dict | None = None,
    ) -> dict:
        """冻结 snapshot → 建 run → 执行到可释放点。返回 run 摘要。"""
        run_id = await self._create(tenant_id, workflow, inputs)
        if self.queue is not None:
            return self._summary(run_id)
        store = await self._stores.resolve(tenant_id)
        ex = DAGExecutor(
            run_id, tenant_id, workflow.dag, store,
            node_runner=self.node_runner, inputs=inputs or {},
        )
        self._executors[run_id] = ex
        outcome = await ex.run()
        await store.update_run(run_id, status=outcome)
        log.info("[%s] create_run -> %s", run_id, outcome)
        return self._summary(run_id)

    async def start_run(
        self, tenant_id: str, workflow: Workflow, inputs: dict | None = None
    ) -> dict:
        """异步启动：建 run + executor，后台任务执行 DAG，立即返回 run_id（UI 轮询用）。"""
        run_id = await self._create(tenant_id, workflow, inputs)
        if self.queue is not None:
            return {"run_id": run_id}
        store = await self._stores.resolve(tenant_id)
        ex = DAGExecutor(
            run_id, tenant_id, workflow.dag, store,
            node_runner=self.node_runner, inputs=inputs or {},
        )
        self._executors[run_id] = ex
        self._tasks[run_id] = asyncio.create_task(self._run_background(run_id, ex, store))
        log.info("[%s] start_run（异步）", run_id)
        return {"run_id": run_id}

    async def _create(self, tenant_id: str, workflow: Workflow, inputs: dict | None) -> str:
        """公共前缀：配额校验（锁内）→ 冻结 snapshot → 建 run →（queue 模式）发布 trigger。"""
        store = await self._stores.resolve(tenant_id)
        # §7.3 repo 直传封堵：加固姿态（共享数据源关闭）下，repo 一律经租户 CMDB MCP
        # 提供——inputs.repos 是"用平台身份操作任意 repo"的绕过入口。
        if inputs and "repos" in inputs and not get_settings().shared_datasources:
            raise InputsValidationError(
                "多租户加固姿态下不接受 inputs.repos 直传（repo 由租户 CMDB MCP 提供；"
                "本地联调可设 AGENTFLOW_SHARED_DATASOURCES=1 解除）"
            )
        lock_key = None
        try:
            if self.lock is not None:
                # 临界区 = 配额检查 + run 行落库（占位）；发布 trigger 在临界区外
                lock_key = await self._acquire_tenant_lock(tenant_id)
            await self._check_quota(tenant_id, store)
            run_id = f"run_{uuid.uuid4().hex[:10]}"
            snapshot_id = await store.save_snapshot(tenant_id, workflow.snapshot())
            await store.create_run(run_id, tenant_id, snapshot_id, inputs or {})
        finally:
            if lock_key is not None:
                await self.lock.release(lock_key)
        if self.queue is not None:
            # 先置 queued 再发布：Worker 接单后才置 running，状态机不回跳
            await store.update_run(run_id, status="queued")
            await self.queue.publish(
                topic_trigger(tenant_id),
                key=run_id,
                message={"type": "trigger", "run_id": run_id, "tenant_id": tenant_id},
            )
            log.info("[%s] 已发布 run.trigger（queue 模式）", run_id)
        return run_id

    # ------------------------------------------------------------------
    # inline 模式后台执行
    # ------------------------------------------------------------------
    async def _run_background(self, run_id: str, ex: DAGExecutor, store: StateStore) -> None:
        """后台执行 DAG 到终态/可释放点；结束后更新 run 状态。"""
        try:
            outcome = await ex.run()
        except WorkflowNodeFailed as exc:
            log.warning("[%s] 后台执行失败: %s", run_id, exc)
            outcome = "failed"
        except asyncio.CancelledError:
            log.info("[%s] 后台任务被取消（stop_run）", run_id)
            await self._mark_cancelled(run_id, ex, store)
            return
        await store.update_run(run_id, status=outcome)
        log.info("[%s] 后台执行结束 -> %s", run_id, outcome)

    async def _mark_cancelled(
        self, run_id: str, ex: DAGExecutor, store: StateStore
    ) -> None:
        """把非终态节点（含 WAITING_APPROVAL）标记为 cancelled 并持久化。

        stop 语义：当前节点跑完即停；待审批节点一并作废，图上显示 cancelled。
        """
        for nid, st in ex.node_states.items():
            if st.get("status") in TERMINAL:
                continue
            ex.node_states[nid] = {"status": "cancelled", "output": None}
            await store.put_node(run_id, ex.tenant_id, nid, ex.node_states[nid])
        await store.update_run(run_id, status="cancelled")

    # ------------------------------------------------------------------
    # 生命周期命令（pause / resume / stop）
    # ------------------------------------------------------------------
    async def pause_run(self, run_id: str, tenant_id: str | None = None) -> None:
        """暂停：queue 模式发布命令；inline 对进程内 executor 请求波间暂停。"""
        store, run, tenant = await self._run_context(run_id, tenant_id)
        if self.queue is not None:
            await self.queue.publish(
                topic_command(tenant),
                key=run_id,
                message={"type": "pause", "run_id": run_id, "tenant_id": tenant},
            )
            return
        ex = self._executors.get(run_id)
        if ex is not None:
            ex.request_pause()

    async def resume_run(self, run_id: str, tenant_id: str | None = None) -> dict:
        """断点续跑（§4.4）：从 checkpoint + 原 snapshot 重建并继续执行。"""
        store, _run, tenant = await self._run_context(run_id, tenant_id)
        await self._check_quota(tenant, store)
        if self.queue is not None:
            await self.queue.publish(
                topic_command(tenant),
                key=run_id,
                message={
                    "type": "resume", "run_id": run_id,
                    "tenant_id": tenant, "trigger": "manual_resume",
                },
            )
            return self._summary(run_id)
        ex = await resume_executor(run_id, tenant, store, node_runner=self.node_runner)
        self._executors[run_id] = ex
        outcome = await ex.run()
        await store.update_run(run_id, status=outcome)
        return self._summary(run_id)

    async def stop_run(self, run_id: str, tenant_id: str | None = None) -> None:
        """停止进行中的 run：queue 模式发布 stop 命令；inline 置 cancelled + 取消任务。"""
        store, _run, tenant = await self._run_context(run_id, tenant_id)
        if self.queue is not None:
            await self.queue.publish(
                topic_command(tenant),
                key=run_id,
                message={"type": "stop", "run_id": run_id, "tenant_id": tenant},
            )
            return
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task  # _run_background 的 CancelledError 分支会 _mark_cancelled
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 后台任务其它异常不阻塞 stop
                pass
        # 兜底：任务已结束（如停在 waiting_approval）或未持有 executor 时直接标记
        ex = self._executors.get(run_id)
        if ex is not None:
            await self._mark_cancelled(run_id, ex, store)
        else:
            await store.update_run(run_id, status="cancelled")

    async def _run_context(
        self, run_id: str, tenant_id: str | None
    ) -> tuple[StateStore, dict | None, str]:
        """解析 run 所在租户库：调用方给 tenant_id 用之（API ctx），否则用缓存 executor
        的租户；单库形态可先按默认库查 run 行取租户；Router 模式必须给 tenant。"""
        ex = self._executors.get(run_id)
        tenant = tenant_id or (ex.tenant_id if ex is not None else None)
        if tenant is not None:
            store = await self._stores.resolve(tenant)
            run = await store.get_run(run_id)
            if run is None:
                raise ValueError(f"run 不存在: {run_id}")
            return store, run, tenant
        if self.store is not None:  # 单库形态：查 run 行取租户
            run = await self.store.get_run(run_id)
            if run is None:
                raise ValueError(f"run 不存在: {run_id}")
            store = await self._stores.resolve(run["tenant_id"])
            return store, run, run["tenant_id"]
        raise ValueError(f"run {run_id} 需提供 tenant_id（多租户路由模式）")

    # ------------------------------------------------------------------
    # 审批（§8.3 CAS + 终态不可逆 + §4.2 default-deny）
    # ------------------------------------------------------------------
    async def approve(
        self,
        run_id: str,
        node_id: str,
        *,
        approved: bool,
        by: str,
        comment: str = "",
        tenant_id: str | None = None,
    ) -> dict:
        """审批（§8.3 CAS），通过/拒绝后继续执行。

        queue 模式：CAS 更新审批与节点 checkpoint 后发布 ``run.command`` resume，
        由 Worker 继续执行（API 进程零占用，多副本安全）。
        """
        ex = self._executors.get(run_id)
        if ex is None:
            store, run, tenant = await self._run_context(run_id, tenant_id)
            self._check_approver(tenant, node_id, by)
            ex = await resume_executor(run_id, tenant, store, node_runner=self.node_runner)
            if self.queue is None:
                self._executors[run_id] = ex
        else:
            tenant = ex.tenant_id
            store = await self._stores.resolve(tenant)
            self._check_approver(tenant, node_id, by)
        out = await ex.approve(node_id, approved=approved, by=by, comment=comment)
        if self.queue is not None:
            await self.queue.publish(
                topic_command(tenant),
                key=run_id,
                message={
                    "type": "resume", "run_id": run_id, "tenant_id": tenant,
                    "trigger": "approval_done", "node_id": node_id,
                },
            )
            log.info("[%s] 审批 %s 完成，已发布 resume 命令", run_id, node_id)
            return {"approval": out, "run_status": "queued", **self._summary(run_id)}
        outcome = await ex.run()
        await store.update_run(run_id, status=outcome)
        return {"approval": out, "run_status": outcome, **self._summary(run_id)}

    # ------------------------------------------------------------------
    def _summary(self, run_id: str) -> dict:
        ex = self._executors.get(run_id)
        return {
            "run_id": run_id,
            "status": {nid: st["status"] for nid, st in ex.node_states.items()} if ex else {},
            "pending_approvals": ex.pending_approvals() if ex else [],
        }
