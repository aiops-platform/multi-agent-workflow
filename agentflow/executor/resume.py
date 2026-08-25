# -*- coding: utf-8 -*-
"""Resume / 断点续跑编排（design §4.4 / §8.5 / §8.6）。

流程可在任意节点停止后恢复：节点级 checkpoint 已持久化（每节点完成后落盘），
Resume 用**原 workflow snapshot**（§8.5 版本冻结）+ 节点 checkpoint 重建执行器，
继续执行未完成节点。副作用节点靠幂等（§8.4）保证不重复副作用。
"""
from __future__ import annotations

import logging

from ..core.dag import DAG
from ..core.workflow import Workflow
from ..statestore.base import StateStore
from .dag_executor import DAGExecutor, NodeRunner

log = logging.getLogger("agentflow.executor.resume")


def load_snapshot_workflow(store: StateStore, snapshot_id: str) -> Workflow:
    """从 StateStore 读 workflow snapshot 重建 Workflow（Resume 用原版本）。"""
    snap = store.get_snapshot(snapshot_id)
    if snap is None:
        raise ValueError(f"snapshot 不存在: {snapshot_id}")
    raw_yaml = snap["workflow_yaml"]
    return Workflow.load_yaml(raw_yaml)


async def resume_executor(
    run_id: str,
    tenant_id: str,
    store: StateStore,
    node_runner: NodeRunner | None = None,
) -> DAGExecutor:
    """从 checkpoint 重建 executor（§4.4 断点续跑的核心入口）。"""
    run = await store.get_run(run_id)
    if run is None:
        raise ValueError(f"run 不存在: {run_id}")
    workflow = load_snapshot_workflow(store, run["workflow_snapshot_id"])
    return await DAGExecutor.from_checkpoint(
        run_id,
        tenant_id,
        workflow.dag,
        store,
        node_runner=node_runner,
        inputs=run.get("inputs"),
    )
