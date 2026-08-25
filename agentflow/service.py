# -*- coding: utf-8 -*-
"""RunService：create / resume / approve 的编排入口。

M0-M2 形态：进程内直接执行（Worker 即调用方）。M5+ 演进为：create 发布
run.trigger，Worker 消费执行，审批完成后经 run.command resume（§8.6 / §4.4）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .core.workflow import Workflow
from .executor.dag_executor import DAGExecutor, NodeRunner
from .executor.resume import resume_executor
from .statestore.base import StateStore

log = logging.getLogger("agentflow.service")


class RunService:
    def __init__(self, store: StateStore, node_runner: NodeRunner | None = None) -> None:
        self.store = store
        self.node_runner = node_runner
        self._executors: dict[str, DAGExecutor] = {}

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
