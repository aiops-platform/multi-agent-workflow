# -*- coding: utf-8 -*-
"""RunService：create / start / resume / approve / stop 的编排入口。

M0-M2 形态：进程内直接执行（Worker 即调用方）。M5+ 演进为：create 发布
run.trigger，Worker 消费执行，审批完成后经 run.command resume（§8.6 / §4.4）。

UI 兼容层（Bug Solve 页）：``start_run`` 立即返回 run_id（后台 asyncio 任务跑 DAG），
``approve`` 在 waiting_approval 时恢复同一 executor 继续，``stop_run`` 取消后台任务。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from .core.dag import TERMINAL
from .core.workflow import Workflow
from .executor.dag_executor import DAGExecutor, NodeRunner, WorkflowNodeFailed
from .executor.resume import resume_executor
from .statestore.base import StateStore

log = logging.getLogger("agentflow.service")


class RunService:
    def __init__(self, store: StateStore, node_runner: NodeRunner | None = None) -> None:
        self.store = store
        self.node_runner = node_runner
        self._executors: dict[str, DAGExecutor] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def create_run(
        self,
        tenant_id: str,
        workflow: Workflow,
        inputs: dict | None = None,
    ) -> dict:
        """冻结 snapshot → 建 run → 执行到可释放点。返回 run 摘要。"""
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        snapshot_id = await self.store.save_snapshot(tenant_id, workflow.snapshot())
        await self.store.create_run(run_id, tenant_id, snapshot_id, inputs or {})

        ex = DAGExecutor(
            run_id, tenant_id, workflow.dag, self.store,
            node_runner=self.node_runner, inputs=inputs or {},
        )
        self._executors[run_id] = ex
        outcome = await ex.run()
        await self.store.update_run(run_id, status=outcome)
        log.info("[%s] create_run -> %s", run_id, outcome)
        return self._summary(run_id)

    async def start_run(
        self, tenant_id: str, workflow: Workflow, inputs: dict | None = None
    ) -> dict:
        """异步启动：建 run + executor，后台任务执行 DAG，立即返回 run_id（UI 轮询用）。"""
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        snapshot_id = await self.store.save_snapshot(tenant_id, workflow.snapshot())
        await self.store.create_run(run_id, tenant_id, snapshot_id, inputs or {})

        ex = DAGExecutor(
            run_id, tenant_id, workflow.dag, self.store,
            node_runner=self.node_runner, inputs=inputs or {},
        )
        self._executors[run_id] = ex
        self._tasks[run_id] = asyncio.create_task(self._run_background(run_id, ex))
        log.info("[%s] start_run（异步）", run_id)
        return {"run_id": run_id}

    async def _run_background(self, run_id: str, ex: DAGExecutor) -> None:
        """后台执行 DAG 到终态/可释放点；结束后更新 run 状态。"""
        try:
            outcome = await ex.run()
        except WorkflowNodeFailed as exc:
            log.warning("[%s] 后台执行失败: %s", run_id, exc)
            outcome = "failed"
        except asyncio.CancelledError:
            log.info("[%s] 后台任务被取消（stop_run）", run_id)
            await self._mark_cancelled(run_id, ex)
            return
        await self.store.update_run(run_id, status=outcome)
        log.info("[%s] 后台执行结束 -> %s", run_id, outcome)

    async def _mark_cancelled(self, run_id: str, ex: DAGExecutor) -> None:
        """把非终态节点（含 WAITING_APPROVAL）标记为 cancelled 并持久化。

        stop 语义：当前节点跑完即停；待审批节点一并作废，图上显示 cancelled。
        """
        for nid, st in ex.node_states.items():
            if st.get("status") in TERMINAL:
                continue
            ex.node_states[nid] = {"status": "cancelled", "output": None}
            await self.store.put_node(run_id, ex.tenant_id, nid, ex.node_states[nid])
        await self.store.update_run(run_id, status="cancelled")

    async def stop_run(self, run_id: str) -> None:
        """停止进行中的 run：置 cancelled + 取消后台任务（若在跑）。"""
        run = await self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"run 不存在: {run_id}")
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
            await self._mark_cancelled(run_id, ex)
        else:
            await self.store.update_run(run_id, status="cancelled")

    async def resume_run(self, run_id: str, tenant_id: str) -> dict:
        """断点续跑（§4.4）：从 checkpoint + 原 snapshot 重建并继续执行。"""
        ex = await resume_executor(run_id, tenant_id, self.store, node_runner=self.node_runner)
        self._executors[run_id] = ex
        outcome = await ex.run()
        await self.store.update_run(run_id, status=outcome)
        return self._summary(run_id)

    async def approve(self, run_id: str, node_id: str, *, approved: bool, by: str, comment: str = "") -> dict:
        """审批（§8.3 CAS），通过/拒绝后继续执行。"""
        ex = self._executors.get(run_id)
        if ex is None:
            run = await self.store.get_run(run_id)
            if run is None:
                raise ValueError(f"run 不存在: {run_id}")
            ex = await resume_executor(run_id, run["tenant_id"], self.store, node_runner=self.node_runner)
            self._executors[run_id] = ex
        out = await ex.approve(node_id, approved=approved, by=by, comment=comment)
        outcome = await ex.run()
        await self.store.update_run(run_id, status=outcome)
        return {"approval": out, "run_status": outcome, **self._summary(run_id)}

    def _summary(self, run_id: str) -> dict:
        ex = self._executors.get(run_id)
        return {
            "run_id": run_id,
            "status": {nid: st["status"] for nid, st in ex.node_states.items()} if ex else {},
            "pending_approvals": ex.pending_approvals() if ex else [],
        }
