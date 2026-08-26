# -*- coding: utf-8 -*-
"""控制面 FastAPI（design §6：POST /run / GET /runs/{id} / POST approve|reject）。

M0-M2 形态：进程内直接执行（无 Worker 池）。JWT → 派生 tenant_id（§9.1）
在 M5 接入 API Gateway；当前接口接受显式 tenant 参数便于本地联调。
"""
from __future__ import annotations

import asyncio

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..approval.notifier import ApprovalNotifier
from ..approval.sweeper import ApprovalSweeper
from ..config import Settings, get_settings
from ..core.workflow import Workflow
from ..executor.dag_executor import WorkflowNodeFailed
from ..queue import build_queue
from ..service import RunService
from ..statestore import build_state_store, connect_state_store

settings: Settings = get_settings()

app = FastAPI(title="agentflow 控制面", version="0.1.0")
service: RunService | None = None
sweeper: ApprovalSweeper | None = None


class RunRequest(BaseModel):
    workflow_yaml: str
    inputs: dict = {}
    tenant_id: str = "local"


class ApproveRequest(BaseModel):
    approved: bool = True
    by: str = "lead-engineer"
    comment: str = ""


def _service() -> RunService:
    global service
    if service is None:
        store = build_state_store(settings)
        # 生命周期：生产由 Worker/API 进程统一 connect；此处懒初始化
        raise RuntimeError("service 未初始化，先调用 init()")
    return service


async def init() -> RunService:
    """应用启动时调用：初始化 StateStore + Queue，并启动审批超时 Sweeper（§8.9）。"""
    global service, sweeper
    store = build_state_store(settings)
    await connect_state_store(store)
    queue = build_queue(settings)
    service = RunService(store)
    sweeper = ApprovalSweeper(store, queue, ApprovalNotifier(), interval=60)
    asyncio.create_task(sweeper.run_forever())
    return service


@app.on_event("startup")
async def _startup() -> None:
    await init()


@app.post("/run")
async def create_run(req: RunRequest) -> dict:
    try:
        workflow = Workflow.load_yaml(req.workflow_yaml)
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=422, detail=f"Workflow 解析失败: {exc}") from exc
    try:
        return await _service().create_run(req.tenant_id, workflow, req.inputs)
    except WorkflowNodeFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    run = await _service().store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    return run


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str, node_id: str, req: ApproveRequest) -> dict:
    try:
        return await _service().approve(
            run_id, node_id, approved=req.approved, by=req.by, comment=req.comment
        )
    except (ValueError, AssertionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/audit")
async def audit(tenant_id: str | None = None, run_id: str | None = None, limit: int = 100) -> list[dict]:
    """审计日志查询（§9.5：tenant_id/tool_name/decision/run_id/node_id/input 脱敏/ts）。"""
    return await _service().store.get_audit_logs(tenant_id=tenant_id, run_id=run_id, limit=limit)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "agentflow-control-plane"}
