# -*- coding: utf-8 -*-
"""控制面 FastAPI（design §6：POST /run / GET /runs/{id} / POST approve|reject）。

M0-M2 形态：进程内直接执行（无 Worker 池）。JWT → 派生 tenant_id（§9.1）
在 M5 接入 API Gateway；当前接口接受显式 tenant 参数便于本地联调。
"""
from __future__ import annotations

import asyncio
import json

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..agents.registry import AGENT_REGISTRY
from ..approval.notifier import ApprovalNotifier
from ..approval.sweeper import ApprovalSweeper
from ..config import Settings, get_settings
from ..core.dag import WAITING_APPROVAL, WorkflowDAGError
from ..core.workflow import Workflow
from ..queue import build_queue
from ..service import RunService
from ..statestore import build_state_store, connect_state_store
from .workflow_store import WorkflowStore

settings: Settings = get_settings()

app = FastAPI(title="agentflow 控制面", version="0.1.0")
service: RunService | None = None
sweeper: ApprovalSweeper | None = None
workflow_store = WorkflowStore(settings.state_db_path)

# CORS：允许前端跨域调用控制面 API。来源可配（AGENTFLOW_CORS_ORIGINS 逗号分隔，默认 *）。
# allow_origins=* 时不可开启 allow_credentials（浏览器规范限制）；JWT 走 Authorization 头不受影响。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    # UI（Bug Solve 页）：已保存 workflow id + ticket JSON
    workflow_id: str | None = None
    ticket: dict = {}
    # 兼容旧用法（脚本/测试）：直接传 YAML + inputs
    workflow_yaml: str | None = None
    inputs: dict = {}
    tenant_id: str = "local"


class ApproveRequest(BaseModel):
    node_id: str | None = None  # 兼容：body 带 node_id，或仍走 query 参数
    approved: bool = True
    by: str = "lead-engineer"
    comment: str = ""


class RejectRequest(BaseModel):
    node_id: str
    by: str = "lead-engineer"
    comment: str = ""


class WorkflowCreate(BaseModel):
    name: str = "未命名"
    yaml: str


class WorkflowPreviewRequest(BaseModel):
    yaml: str


def _workflow_graph(wf: Workflow) -> dict:
    """从 Workflow 模型提取节点/边结构（供前端画图，对齐 agentflow 后端契约）。"""
    return {
        "name": wf.name,
        "nodes": [
            {"id": nid, "agent": node.agent, "kind": node.kind}
            for nid, node in wf.nodes.items()
        ],
        "edges": [
            {"from": e.source, "to": e.target, "when": e.when}
            for e in wf.dag.edges
        ],
    }


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
    await workflow_store.connect()
    store = build_state_store(settings)
    await connect_state_store(store)
    queue = build_queue(settings)
    kwargs: dict = {}
    if settings.deepseek_api_key:
        # 有 Key → 真实 agent：等价于参考 agentflow 把 AgentRuntime 注入 DAGExecutor；
        # 无 Key 时保持默认 mock（_default_runner），页面全绿但 token/cost 诚实为 0。
        from ..agents.runner import AgentNodeRunner
        from ..agents.scopes import build_model

        kwargs["node_runner"] = AgentNodeRunner(build_model(settings))
        print("[agentflow] node_runner=agent（DeepSeek）：Bug Solve 页将真实调用 agent")
    service = RunService(store, **kwargs)
    sweeper = ApprovalSweeper(store, queue, ApprovalNotifier(), interval=60)
    asyncio.create_task(sweeper.run_forever())
    return service


@app.on_event("startup")
async def _startup() -> None:
    await init()


@app.post("/run")
async def create_run(req: RunRequest) -> dict:
    """触发一次 run。UI 契约（agentflow 兼容）：立即返回 ``{run_id, status:"started"}``，
    后台任务执行 DAG；轮询方用 ``GET /runs/{run_id}`` 取进度/结果。

    - ``workflow_id``：已保存 workflow（workflow_store）——Bug Solve 页主路径
    - ``workflow_yaml``：直接传 YAML 文本（兼容旧用法，脚本/CLI）
    """
    if req.workflow_id:
        wf_row = await workflow_store.get(req.workflow_id)  # get() 内部惰性 connect
        if wf_row is None:
            raise HTTPException(status_code=404, detail="workflow 不存在")
        try:
            workflow = Workflow.load_yaml(wf_row["yaml"])
        except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
            raise HTTPException(status_code=400, detail=f"Workflow 解析失败: {exc}") from exc
    elif req.workflow_yaml:
        try:
            workflow = Workflow.load_yaml(req.workflow_yaml)
        except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
            raise HTTPException(status_code=422, detail=f"Workflow 解析失败: {exc}") from exc
    else:
        raise HTTPException(status_code=400, detail="需提供 workflow_id 或 workflow_yaml")
    inputs = req.ticket or req.inputs or {}
    out = await _service().start_run(req.tenant_id or "local", workflow, inputs)
    return {"run_id": out["run_id"], "status": "started"}


# ── workflow CRUD（流程配置页面：保存/复用/预览）──
# 注意：/workflows/preview 必须在 /workflows/{wid} 之前注册，避免被捕获为 wid。

@app.post("/workflows/preview")
async def preview_workflow(req: WorkflowPreviewRequest) -> dict:
    """解析 YAML 并返回图结构（前端预览用，不落库）。"""
    try:
        wf = Workflow.load_yaml(req.yaml)
    except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
        raise HTTPException(status_code=400, detail=f"Workflow 解析失败: {exc}") from exc
    return {"name": wf.name, "graph": _workflow_graph(wf)}


@app.post("/workflows")
async def create_workflow(req: WorkflowCreate) -> dict:
    """保存一条 workflow（校验 YAML 合法性），返回 id + graph。"""
    try:
        wf = Workflow.load_yaml(req.yaml)
    except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
        raise HTTPException(status_code=400, detail=f"Workflow 解析失败: {exc}") from exc
    wid = await workflow_store.save(req.name, req.yaml)
    return {"id": wid, "name": req.name, "graph": _workflow_graph(wf)}


@app.get("/workflows")
async def list_workflows() -> list[dict]:
    return await workflow_store.list()


@app.get("/workflows/{wid}")
async def get_workflow(wid: str) -> dict:
    wf_row = await workflow_store.get(wid)
    if wf_row is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    try:
        wf = Workflow.load_yaml(wf_row["yaml"])
        graph = _workflow_graph(wf)
    except (ValueError, yaml.YAMLError, WorkflowDAGError):
        graph = {}
    return {**wf_row, "graph": graph}


@app.put("/workflows/{wid}")
async def update_workflow(wid: str, req: WorkflowCreate) -> dict:
    """更新一条 workflow（校验 YAML 合法性）。"""
    try:
        wf = Workflow.load_yaml(req.yaml)
    except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
        raise HTTPException(status_code=400, detail=f"Workflow 解析失败: {exc}") from exc
    if not await workflow_store.update(wid, req.name, req.yaml):
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return {"id": wid, "name": req.name, "graph": _workflow_graph(wf)}


@app.delete("/workflows/{wid}")
async def delete_workflow(wid: str) -> dict:
    if not await workflow_store.delete(wid):
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return {"ok": True}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    """聚合 run 详情（UI 轮询契约，对齐 agentflow 后端）：图 + 节点状态 + 统计 + 待审批。

    - ``graph``：从原 snapshot 重建（workflow 删除也不影响已跑 run）
    - ``nodes[id]``：{status, output, params, tokens, cost, prompt}；mock 无 LLM → tokens/cost 为 0
    - ``pending_approvals``：[{node_id, trigger, upstream}]，upstream 取上游节点输出
    """
    service = _service()
    run = await service.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")

    # 图：从 snapshot 重建（Resume/展示不受 workflow 变更影响）
    graph = {}
    wf = None
    try:
        snap = await service.store.get_snapshot(run["workflow_snapshot_id"])
        if snap:
            wf = Workflow.load_yaml(snap["workflow_yaml"])
            graph = _workflow_graph(wf)
    except (ValueError, yaml.YAMLError, WorkflowDAGError):
        graph = {}

    nodes_raw = await service.store.get_nodes(run_id)
    nodes: dict[str, dict] = {}
    total_tokens = 0
    total_cost = 0.0
    for nid, cp in nodes_raw.items():
        params = cp.get("params") or {}
        nodes[nid] = {
            "status": cp.get("status"),
            "output": cp.get("output"),
            "params": params,
            "tokens": cp.get("tokens", 0),
            "cost": cp.get("cost", 0.0),
            "prompt": json.dumps(params, ensure_ascii=False),
        }
        total_tokens += cp.get("tokens", 0)
        total_cost += cp.get("cost", 0.0)

    # 待审批：node checkpoint 状态 == WAITING_APPROVAL（upstream 取上游节点输出）
    pending = []
    for nid, cp in nodes_raw.items():
        if cp.get("status") != WAITING_APPROVAL:
            continue
        params = cp.get("params") or {}
        upstreams = wf.dag.nodes[nid].upstreams if wf else []
        upstream_out = {u: nodes_raw.get(u, {}).get("output") for u in upstreams}
        pending.append({
            "node_id": nid,
            "trigger": params.get("trigger"),
            "upstream": upstream_out,
        })

    status_map = {
        "done": "success",
        "running": "running",
        "failed": "failed",
        "cancelled": "cancelled",
        "waiting_approval": "waiting_approval",
    }
    return {
        "run_id": run_id,
        "workflow": graph.get("name"),
        "graph": graph,
        "status": status_map.get(run["status"], run["status"]),
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "nodes": nodes,
        "pending_approvals": pending,
    }


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str, node_id: str | None = None, req: ApproveRequest | None = None) -> dict:
    """通过某审批节点。body ``{node_id}``（UI 契约）或 query ``?node_id=``（旧兼容）。"""
    rid = req.node_id if (req and req.node_id) else node_id
    if not rid:
        raise HTTPException(status_code=400, detail="需提供 node_id")
    try:
        return await _service().approve(
            run_id, rid,
            approved=(req.approved if req else True),
            by=(req.by if req else "lead-engineer"),
            comment=(req.comment if req else ""),
        )
    except (ValueError, AssertionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/runs/{run_id}/reject")
async def reject(run_id: str, req: RejectRequest) -> dict:
    """驳回某审批节点（UI 契约）。"""
    try:
        return await _service().approve(
            run_id, req.node_id, approved=False, by=req.by, comment=req.comment
        )
    except (ValueError, AssertionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    """停止进行中的 run（置 cancelled + 取消后台任务）。"""
    try:
        await _service().stop_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id}


@app.get("/audit")
async def audit(tenant_id: str | None = None, run_id: str | None = None, limit: int = 100) -> list[dict]:
    """审计日志查询（§9.5：tenant_id/tool_name/decision/run_id/node_id/input 脱敏/ts）。"""
    return await _service().store.get_audit_logs(tenant_id=tenant_id, run_id=run_id, limit=limit)


@app.get("/agents")
async def agents() -> list[dict]:
    """Agent 编队列表（静态平台元数据，读取 AGENT_REGISTRY；与 /health 同级无鉴权）。

    返回 [{name, description, tools, stage}]，tools 为该 agent 在 Tool Registry 中可见的工具名，
    stage 为流水线阶段（detect/diagnose/fix/verify/deliver/learn），供前端舰队分组展示。
    """
    return [
        {"name": spec.name, "description": spec.description,
         "tools": [t.name for t in spec.tools],
         "stage": spec.stage}
        for spec in AGENT_REGISTRY.values()
    ]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "agentflow-control-plane"}
