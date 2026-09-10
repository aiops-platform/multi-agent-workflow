"""控制面 FastAPI（design §6：POST /run / GET /runs/{id} / approve|reject / pause|resume）。

- **认证**（§9.1）：``get_tenant_context`` 依赖从 Bearer JWT 派生 tenant_id；
  未配置 ``AGENTFLOW_JWT_SECRET`` 时回退显式传参（dev 联调，启动告警）。
- **执行模式**（§6/§8.6）：``run_mode=inline`` 进程内直跑（默认）；``run_mode=queue``
  只发布 run.trigger/run.command，由 Worker 消费（queue=memory 时进程内后台 Worker，
  queue=kafka 时独立进程 ``python -m agentflow.worker``）。
- **多租户**（§9.2/§9.3）：run 数据按派生租户隔离（跨租户一律 404）；配额/审批人
  白名单由 ``TenantRegistry`` 提供。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..agents.agent_config import AgentConfigResolver
from ..agents.mcp_manager import MCPClientManager
from ..agents.registry import DIAGNOSE_AGENTS, FIX_AGENTS, get_agent_spec
from ..agents.tools import tools_for_agent
from ..approval.notifier import ApprovalNotifier
from ..approval.sweeper import ApprovalSweeper
from ..config import Settings, get_settings
from ..core.dag import WAITING_APPROVAL, WorkflowDAGError
from ..core.workflow import Workflow
from ..executor.dag_executor import ApprovalRaceError
from ..lock import build_lock
from ..queue import build_queue
from ..service import ApproverNotAllowed, InputsValidationError, RunService, TenantQuotaExceeded
from ..statestore.router import TenantStoresRouter
from ..tenants import TenantRegistry, async_bootstrap_tenants
from ..worker import WorkerPool
from .agent_store import (
    AgentConfigStore,
    build_agent_config_store,
)
from .auth import TenantContext, get_tenant_context
from .management_store import build_management_store
from .mcp_store import MCPStore, build_mcp_store
from .workflow_store import WorkflowStore, build_workflow_store

settings: Settings = get_settings()
# 租户注册表：import 时为 builtin 默认（dev）；init() 从管理库重建（v5.3 §5.2）
tenant_registry = TenantRegistry.builtin()
# 管理库 + 租户库路由 + 配额锁：init() 装配；测试可 monkeypatch 模块全局替换
management_store = build_management_store(settings)
stores_router: TenantStoresRouter | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """取代弃用的 @app.on_event：启动跑 init()（StateStore/Queue + 审批 Sweeper），关闭回收 MCP 连接。

    注：init()/mcp_manager 在本文件后文才定义/赋值，但 lifespan 由 ASGI server 在模块
    完全加载后才调用，届时模块全局已就绪，无前向引用问题。
    """
    await init()
    yield
    await mcp_manager.close_all()


app = FastAPI(title="agentflow 控制面", version="0.1.0", lifespan=lifespan)
service: RunService | None = None
sweeper: ApprovalSweeper | None = None
worker: Worker | None = None  # run_mode=queue + memory 队列时的进程内 Worker
_worker_task: asyncio.Task | None = None
# 控制面配置存储（workflows/mcp_servers/agent_configs）：state_store=postgres 时落 PG，
# 否则沿用本地 sqlite（构造不做 DB/I/O，惰性 connect；测试可 monkeypatch 模块全局）。
workflow_store = build_workflow_store(settings)
mcp_store = build_mcp_store(settings)
agent_config_store = build_agent_config_store(settings)
# 运行时 MCP client 管理器（持有同一个 store 引用，读取 enabled=1 配置）
mcp_manager = MCPClientManager(mcp_store)
# AgentSpec 配置解析器（DB 覆盖 + 内置静态回退）：init()/CRUD 后经 _reload_agent_config_resolver 重建，
# 并重接 mcp_manager.server_ids_for（agent→MCP server 绑定，server 粒度）。
_agent_config_resolver: AgentConfigResolver | None = None
_resolver_cache: dict[str, tuple[int, AgentConfigResolver]] = {}
_resolver_generation = 0

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
        # 生命周期：生产由 Worker/API 进程统一 connect；此处懒初始化
        raise RuntimeError("service 未初始化，先调用 init()")
    return service


async def _run_for_tenant(run_id: str, ctx: TenantContext) -> dict:
    """读 run 并强制租户隔离（§9.2）：跨租户一律 404（不泄漏存在性）。

    Router 模式下 run 在该租户自己的库里——查不到即 404（隔离由构造保证）。"""
    store = await _service().store_for(ctx.tenant_id)
    run = await store.get_run(run_id)
    if run is None or run["tenant_id"] != ctx.tenant_id:
        raise HTTPException(status_code=404, detail="run 不存在")
    return run


async def _reload_agent_config_resolver(tenant_id: str | None = None) -> AgentConfigResolver:
    """重建 AgentSpec 解析器（Router 模式读该租户库的覆盖行），并重接 mcp_manager。

    init（seed 后）与每次 /agent-configs CRUD 后调用，保证运行时 + GET /agents 读到最新
    DB 覆盖/自定义 agent。构造不做 DB I/O 之外的重活（内存行索引）。
    """
    global _agent_config_resolver, _resolver_generation
    _resolver_generation += 1  # per-tenant 代际缓存失效（CRUD 后重建）
    if stores_router is not None:
        bundle = await stores_router.get(tenant_id or "local")
        rows = await bundle.agent_config.list()
    else:  # 单租户回退（测试/未 init）
        rows = await agent_config_store.list()
    _agent_config_resolver = AgentConfigResolver(rows)
    async def _tenant_server_ids_for(agent_name: str, tenant_id: str | None = None):
        resolver = await _agent_config_provider(tenant_id)
        return resolver.server_ids_for(agent_name)

    mcp_manager.server_ids_for = _tenant_server_ids_for
    # 运行中 node_runner 持 init() 时传入的 resolver 对象快照：CRUD 只重建模块全局 + 重接
    # mcp_manager，若不把新实例重指向 runner，启动后新建/补写 system_prompt 的 agent 在后续 run
    # 里仍按旧快照解析（NULL→默认提示），表现为输出退化（如只有默认提示没有 JSON 契约 → {}）。
    runner = getattr(service, "node_runner", None)
    if runner is not None and getattr(runner, "agent_config", None) is not None:
        runner.agent_config = _agent_config_resolver
    return _agent_config_resolver


def _effective_agent_resolver() -> AgentConfigResolver:
    """读当前全局解析器；未 init（测试无 lifespan）时退化为空配置 → 纯静态内置视图。"""
    return _agent_config_resolver if _agent_config_resolver is not None else AgentConfigResolver([])


async def _migrate_sqlite_config_to_pg() -> None:
    """本地 SQLite → PostgreSQL 一次性迁移（仅 state_store=postgres、源库存在时执行）。

    把历史 ``data/agentflow.db`` 里的控制面配置（workflows / mcp_servers）搬进 PG，
    保证切换生产后端后页面数据仍可见。**目标表非空则跳过**（幂等，可重复启动）。
    源用临时 sqlite store 只读；目标复用 mcp_store/workflow_store（PG，已 connect 建表）。
    """
    if not Path(settings.state_db_path).exists():
        return

    async def _copy_workflows() -> None:
        if await workflow_store.list():  # PG 已有数据 → 已迁移或生产自建，跳过
            return
        src = WorkflowStore(settings.state_db_path)
        await src.connect()
        try:
            for row in await src.list():
                full = await src.get(row["id"])  # list 不含 yaml，需逐条 get
                await workflow_store.save(full["name"], full["yaml"])
        finally:
            await src.close()

    async def _copy_mcp() -> None:
        if await mcp_store.list():
            return
        src = MCPStore(settings.state_db_path)
        await src.connect()
        try:
            # 整行交给 PgMCPStore.save：_to_row 忽略 id/created_at，重新生成新 id（无外键引用）
            for row in await src.list():
                await mcp_store.save(row)
        finally:
            await src.close()

    async def _copy_agent_configs() -> None:
        if await agent_config_store.list():
            return
        src = AgentConfigStore(settings.state_db_path)
        await src.connect()
        try:
            # 整行交给 PgAgentConfigStore.save（name 为 PK，直接带 name/覆盖字段搬）
            for row in await src.list():
                await agent_config_store.save(row)
        finally:
            await src.close()

    await _copy_workflows()
    await _copy_mcp()
    await _copy_agent_configs()
    print("[agentflow] 已把本地 SQLite 的 workflows/mcp_servers/agent_configs 配置迁移到 PostgreSQL")


def build_cmdb():
    """构建租户 CMDB（§9.4 TenantMappingProvider）。

    本地 testbed 用 ``MockCmdbProvider`` + ``workspace.prepare.default_cmdb()`` 的
    service→repo 映射（file:// 本地源）。**诊断段与工作区准备共用同一映射**——
    code-locator 输出的 repo_url 与工作区实际克隆的仓库必须一致。
    """
    from ..workspace.cmdb import MockCmdbProvider
    from ..workspace.prepare import default_cmdb

    return MockCmdbProvider(default_cmdb())


async def init() -> RunService:
    """应用启动时调用：管理库/租户路由 + StateStore + Queue + 审批超时 Sweeper（§8.9）。"""
    global service, sweeper, worker, _worker_task, stores_router, tenant_registry
    app.state.settings = settings  # auth 依赖读取（get_tenant_context）
    # ── 管理库（v5.3 §5.2）：bootstrap 种子 → 租户注册表 ──
    await management_store.connect()
    await async_bootstrap_tenants(management_store, settings)
    tenant_registry = await TenantRegistry.from_management(management_store)
    # ── 租户库路由（P4）：tenant_id → TenantStores（state+workflow+mcp+agent_config）──
    stores_router = TenantStoresRouter(settings, management_store)

    # per-tenant AgentSpec 解析器（代际缓存：CRUD 后 _reload 递增 → 失效重建）
    async def _agent_config_provider(tenant_id: str | None) -> AgentConfigResolver:
        key = tenant_id or "local"
        hit = _resolver_cache.get(key)
        if hit is not None and hit[0] == _resolver_generation:
            return hit[1]
        bundle = await stores_router.get(key)
        resolver = AgentConfigResolver(await bundle.agent_config.list())
        _resolver_cache[key] = (_resolver_generation, resolver)
        return resolver

    # per-tenant mcp store 路由（§7：租户的 mcp_servers 表在租户自己的库）
    async def _mcp_store_provider(tenant_id: str | None):
        bundle = await stores_router.get(tenant_id or "local")
        return bundle.mcp

    mcp_manager.stores_provider = _mcp_store_provider
    # mcp_manager：预加载全局默认租户的 MCP 连接（per-tenant 缓存在批 C 接入 runner）
    await mcp_manager.load()
    if not settings.jwt_secret:
        print(
            "[agentflow][WARN] 未配置 AGENTFLOW_JWT_SECRET：认证关闭，tenant_id 由客户端提交"
            "（§9.1 禁止作为生产授权依据，仅限本地联调）"
        )
    queue = build_queue(settings)
    kwargs: dict = {}
    if settings.deepseek_api_key:
        # 有 Key → 真实 agent：等价于参考 agentflow 把 AgentRuntime 注入 DAGExecutor；
        # 无 Key 时保持默认 mock（_default_runner），页面全绿但 token/cost 诚实为 0。
        from ..agents.runner import AgentNodeRunner
        from ..agents.scopes import build_model

        resolver = await _reload_agent_config_resolver()
        kwargs["node_runner"] = AgentNodeRunner(
            build_model(settings),
            mcp_manager=mcp_manager,
            agent_config=resolver,
            agent_config_provider=_agent_config_provider,
            # 数据源查询全部经 MCP（mcp_manager 注入的 client，design-v5.5）
            # 租户 CMDB：code-locator 的 locate_code 走它解析 service→repo（§9.4）
            cmdb=build_cmdb(),
        )
        print("[agentflow] node_runner=agent（DeepSeek）：Bug Solve 页将真实调用 agent")
    queue_mode = settings.run_mode == "queue"
    service = RunService(
        stores_router,
        queue=queue if queue_mode else None,
        tenant_registry=tenant_registry,
        lock=build_lock(settings),
        **kwargs,
    )
    sweeper = ApprovalSweeper(
        stores_router,
        queue,
        ApprovalNotifier(),
        interval=60,
        tenants_provider=_active_tenant_ids,
    )
    asyncio.create_task(sweeper.run_forever())
    if queue_mode:
        if settings.queue == "memory":
            # 单进程形态：Worker 以后台任务运行（与独立进程行为一致）
            worker = WorkerPool(
                stores_router, queue, node_runner=service.node_runner,
                tenants_provider=_active_tenant_ids,
            )
            _worker_task = asyncio.create_task(worker.run_forever())
            print("[agentflow] run_mode=queue + memory：进程内 Worker 已启动")
        else:
            print(
                f"[agentflow] run_mode=queue（{settings.queue}）：请单独运行 `python -m agentflow.worker`"
            )
    return service


async def _active_tenant_ids() -> list[str]:
    """sweeper 的租户清单提供者（管理库 active 租户）。"""
    rows = await management_store.list_tenants(status="active")
    return [r["tenant_id"] for r in rows]


async def _control_stores(ctx: TenantContext | None):
    """控制面配置存储解析（v5.3 §5.1：配置表随租户库走）。

    - init 后（Router 就绪）按 ctx 租户取其租户库 bundle；
    - 未 init/测试回退模块全局单例（单租户形态，test_*_api 依赖此路径）。
    """
    if stores_router is not None and ctx is not None:
        return await stores_router.get(ctx.tenant_id)

    class _GlobalStores:  # 单租户回退形状（与 TenantStores 同字段）
        tenant_id = "local"

        def __init__(self) -> None:
            self.state = None
            self.workflow = workflow_store
            self.mcp = mcp_store
            self.agent_config = agent_config_store

    return _GlobalStores()


@app.post("/run")
async def create_run(req: RunRequest, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """触发一次 run。UI 契约（agentflow 兼容）：立即返回 ``{run_id, status:"started"}``，
    后台任务执行 DAG；轮询方用 ``GET /runs/{run_id}`` 取进度/结果。

    - ``workflow_id``：已保存 workflow（workflow_store）——Bug Solve 页主路径
    - ``workflow_yaml``：直接传 YAML 文本（兼容旧用法，脚本/CLI）
    - 租户：JWT 模式由 token claim 派生（body ``tenant_id`` 忽略，§9.1）；
      dev 模式回退 body ``tenant_id`` / ``X-Tenant-ID`` 头
    """
    if req.workflow_id:
        # workflow 定义存该租户自己的库（v5.3 §5.1：配置表随租户库走）
        cs = await _control_stores(ctx)
        wf_row = await cs.workflow.get(req.workflow_id)
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
    tenant_id = req.tenant_id if (not ctx.is_jwt and req.tenant_id) else ctx.tenant_id
    try:
        out = await _service().start_run(tenant_id, workflow, inputs)
    except TenantQuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except InputsValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
async def create_workflow(req: WorkflowCreate, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """保存一条 workflow（校验 YAML 合法性），返回 id + graph。"""
    try:
        wf = Workflow.load_yaml(req.yaml)
    except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
        raise HTTPException(status_code=400, detail=f"Workflow 解析失败: {exc}") from exc
    cs = await _control_stores(ctx)
    wid = await cs.workflow.save(req.name, req.yaml)
    return {"id": wid, "name": req.name, "graph": _workflow_graph(wf)}


@app.get("/workflows")
async def list_workflows(ctx: TenantContext = Depends(get_tenant_context)) -> list[dict]:
    cs = await _control_stores(ctx)
    return await cs.workflow.list()


@app.get("/workflows/{wid}")
async def get_workflow(wid: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    cs = await _control_stores(ctx)
    wf_row = await cs.workflow.get(wid)
    if wf_row is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    try:
        wf = Workflow.load_yaml(wf_row["yaml"])
        graph = _workflow_graph(wf)
    except (ValueError, yaml.YAMLError, WorkflowDAGError):
        graph = {}
    return {**wf_row, "graph": graph}


@app.put("/workflows/{wid}")
async def update_workflow(
    wid: str, req: WorkflowCreate, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """更新一条 workflow（校验 YAML 合法性）。"""
    try:
        wf = Workflow.load_yaml(req.yaml)
    except (ValueError, yaml.YAMLError, WorkflowDAGError) as exc:
        raise HTTPException(status_code=400, detail=f"Workflow 解析失败: {exc}") from exc
    cs = await _control_stores(ctx)
    if not await cs.workflow.update(wid, req.name, req.yaml):
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return {"id": wid, "name": req.name, "graph": _workflow_graph(wf)}


@app.delete("/workflows/{wid}")
async def delete_workflow(wid: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    cs = await _control_stores(ctx)
    if not await cs.workflow.delete(wid):
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return {"ok": True}


# ── MCP Server 配置 CRUD（SIP「MCP Server 配置」页：配置通用 MCP server）──
# 运行期：MCPStore（同库异表 CRUD）+ MCPClientManager（解析为 AgentScope MCPClient + 热刷新）。
# server 记录不存 agent 绑定（原 agents 字段已移除）；绑定以 agent 为主表建模，运行时经注入的
# AgentConfigResolver.server_ids_for 按 agent 所选 server 过滤——v1.12.1 起「没配置=没有 server」，
# agent 需显式绑定才有 MCP 工具（未注入 resolver 的独立用法才回退到全部 enabled）。
# 注意：静态子路径 /mcp-servers/test 先于 /{mid} 系列注册（同 /workflows/preview 教训）。

_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# create/update 自动拉工具快照的整体超时（秒）：连不上目标 server 时短超时快速降级为 null，
# 不让「保存配置」被远端死活拖慢（独立于「测试连接」的 10 秒）。
_MCP_SNAPSHOT_TIMEOUT = 3.0


class MCPServerCreate(BaseModel):
    """创建一条 MCP server 配置。name 也是 MCPClient.name，须 ^[a-zA-Z0-9_-]+$。"""

    name: str
    transport: str  # 'stdio' | 'http'
    config: dict  # stdio{command,args,env,cwd} / http{url,headers,timeout}
    is_stateful: bool | None = None  # stdio 强制 True；http 默认 False(stateless)
    enable_tools: list[str] | None = None
    disable_tools: list[str] | None = None
    tools: list[dict[str, Any]] | None = None  # tools/list 快照（[{name,description,read_only,llm_name}]），null=未知
    enabled: bool = True


class MCPServerUpdate(MCPServerCreate):
    """更新（SIP 表单提交完整对象）。tools 缺省(=None)时保留已存快照，不覆盖。"""


class MCPTestRequest(BaseModel):
    """「测试连接」请求体（不落库，仅传输 + config + 工具过滤）。

    ``name`` 可选：用户已填 server 名时带上，让探测出的工具 LLM 名（``mcp__{name}__…``）
    与实际保存后的前缀一致；缺省用占位名 ``mcp-test``。
    """

    name: str | None = None
    transport: str
    config: dict
    is_stateful: bool | None = None
    enable_tools: list[str] | None = None
    disable_tools: list[str] | None = None


def _validate_mcp_transport(transport: str, config: dict, is_stateful: bool | None) -> bool:
    """校验 transport/config，返回规整后的 is_stateful（stdio 强制 True）。失败抛 400 中文。"""
    if transport not in ("stdio", "http"):
        raise HTTPException(status_code=400, detail="transport 仅支持 stdio 或 http")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="config 必须是 JSON 对象")
    if transport == "stdio":
        if not isinstance(config.get("command"), str) or not config["command"].strip():
            raise HTTPException(status_code=400, detail="stdio 配置需提供 command（子进程启动命令）")
        return True  # stdio 强制 stateful（AgentScope 硬性约束）
    if not isinstance(config.get("url"), str) or not config["url"].strip():
        raise HTTPException(status_code=400, detail="http 配置需提供 url（MCP server 地址）")
    return bool(is_stateful)  # http 默认 stateless


def _validate_mcp_tools(enable_tools, disable_tools) -> None:
    if enable_tools and disable_tools:
        overlap = set(enable_tools) & set(disable_tools)
        if overlap:
            raise HTTPException(status_code=400, detail=f"enable_tools 与 disable_tools 不能重叠: {sorted(overlap)}")


def _mcp_create_row(req: MCPServerCreate) -> dict:
    if not _MCP_NAME_RE.fullmatch(req.name):
        raise HTTPException(
            status_code=400,
            detail="name 仅支持字母/数字/下划线/中划线（同时是 MCPClient.name，LLM 侧工具名前缀）",
        )
    _validate_mcp_tools(req.enable_tools, req.disable_tools)
    stateful = _validate_mcp_transport(req.transport, req.config, req.is_stateful)
    return {
        "name": req.name,
        "transport": req.transport,
        "config": req.config,
        "is_stateful": stateful,
        "enable_tools": req.enable_tools,
        "disable_tools": req.disable_tools,
        "tools": req.tools,
        "enabled": req.enabled,
    }


async def _mcp_snapshot(data: dict, tenant_id: str | None = None) -> list[dict[str, Any]] | None:
    """best-effort 拉一次 tools/list 快照；失败返回 None（不抛，不阻断保存）。

    只在请求未显式携带 tools 时调用（create/update 缺省自动探测）；目标 server 不可达/慢 →
    短超时收敛为 None，配置照常保存。
    """
    if data.get("tools") is not None:
        return data["tools"]
    try:
        res = await mcp_manager.test_connection(data, timeout=_MCP_SNAPSHOT_TIMEOUT)
    except Exception:  # noqa: BLE001 —— 任何异常都降级为 None
        return None
    return res.get("tools") if res.get("ok") else None


@app.post("/mcp-servers", status_code=201)
async def create_mcp_server(req: MCPServerCreate, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """保存一条 MCP server 配置（存该租户库）并热刷新 client，返回 id。

    ``tools`` 未显式携带时 best-effort 连一次 tools/list 落快照；目标不可达则存 null，
    不阻断保存（快照随时可经 GET /{mid}/tools 重新拉取）。
    """
    cs = await _control_stores(ctx)
    data = _mcp_create_row(req)
    data["tools"] = await _mcp_snapshot(data, ctx.tenant_id)
    try:
        mid = await cs.mcp.save(data)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="name 已存在（MCP server 名需唯一）") from None
    await mcp_manager.refresh_server(mid, tenant_id=ctx.tenant_id)  # 重建 + best-effort connect
    return {"id": mid}


@app.get("/mcp-servers")
async def list_mcp_servers(ctx: TenantContext = Depends(get_tenant_context)) -> list[dict]:
    cs = await _control_stores(ctx)
    return await cs.mcp.list()


@app.post("/mcp-servers/test")
async def test_mcp_server(req: MCPTestRequest) -> dict:
    """临时建 client 连一次并列出工具（不落库）。连不上/超时返回 {ok:false}，不抛 500。"""
    _validate_mcp_tools(req.enable_tools, req.disable_tools)
    stateful = _validate_mcp_transport(req.transport, req.config, req.is_stateful)
    name = req.name or "mcp-test"
    if not _MCP_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="name 仅支持字母/数字/下划线/中划线（同时是 MCPClient.name，LLM 侧工具名前缀）",
        )
    row = {
        "name": name,
        "transport": req.transport,
        "config": req.config,
        "is_stateful": stateful,
        "enable_tools": req.enable_tools,
        "disable_tools": req.disable_tools,
    }
    return await mcp_manager.test_connection(row)


@app.get("/mcp-servers/{mid}")
async def get_mcp_server(mid: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    cs = await _control_stores(ctx)
    row = await cs.mcp.get(mid)
    if row is None:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    return row


@app.put("/mcp-servers/{mid}")
async def update_mcp_server(
    mid: str, req: MCPServerUpdate, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """更新并热刷新 client（enabled=0 → refresh 时只 evict 不重建）。

    ``tools`` 缺省(=None)时保留已存快照（不 fetch、不覆盖）；显式携带则整体替换。
    """
    cs = await _control_stores(ctx)
    existing = await cs.mcp.get(mid)
    if existing is None:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    data = _mcp_create_row(req)
    if data["tools"] is None:
        data["tools"] = existing.get("tools")  # 未带 → 保留旧快照
    try:
        hit = await cs.mcp.update(mid, data)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="name 已存在（MCP server 名需唯一）") from None
    if not hit:  # 并发删除兜底
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    await mcp_manager.refresh_server(mid, tenant_id=ctx.tenant_id)
    return {"ok": True, "id": mid}


@app.delete("/mcp-servers/{mid}")
async def delete_mcp_server(mid: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """删除并 evict 对应 client（关闭 stateful 连接 / 杀 stdio 子进程）。"""
    cs = await _control_stores(ctx)
    if not await cs.mcp.delete(mid):
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    await mcp_manager.refresh_server(mid, tenant_id=ctx.tenant_id)  # 已无该行 → 仅 evict
    return {"ok": True}


@app.get("/mcp-servers/{mid}/tools")
async def mcp_server_tools(mid: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """「重新拉取」：已存 server 实时连接并列工具（复用 test_connection 形状）。

    成功后把最新快照回写 tools 列（供列表/详情离线展示）；失败保留已存快照不动。
    """
    cs = await _control_stores(ctx)
    row = await cs.mcp.get(mid)
    if row is None:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    res = await mcp_manager.test_connection(row)
    if res.get("ok") and res.get("tools") is not None:
        await cs.mcp.update_tools(mid, res["tools"])
    return res


@app.get("/runs/{run_id}")
async def get_run(run_id: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """聚合 run 详情（UI 轮询契约，对齐 agentflow 后端）：图 + 节点状态 + 统计 + 待审批。

    - ``graph``：从原 snapshot 重建（workflow 删除也不影响已跑 run）
    - ``nodes[id]``：{status, output, params, tokens, cost, prompt}；mock 无 LLM → tokens/cost 为 0
    - ``pending_approvals``：[{node_id, trigger, upstream}]，upstream 取上游节点输出
    - 租户隔离：跨租户访问一律 404（§9.2）
    """
    service = _service()
    run = await _run_for_tenant(run_id, ctx)
    store = await service.store_for(ctx.tenant_id)

    # 图：从原 snapshot 重建（Resume/展示不受 workflow 变更影响）
    graph = {}
    wf = None
    try:
        snap = await store.get_snapshot(run["workflow_snapshot_id"])
        if snap:
            wf = Workflow.load_yaml(snap["workflow_yaml"])
            graph = _workflow_graph(wf)
    except (ValueError, yaml.YAMLError, WorkflowDAGError):
        graph = {}

    nodes_raw = await store.get_nodes(run_id)
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


@app.get("/runs/{run_id}/traces")
async def get_run_traces(
    run_id: str,
    node_id: str | None = None,
    kind: str | None = None,
    limit: int = 500,
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[dict]:
    """节点级执行明细（node_traces 流水，按 (node_id, seq) 升序）。

    kind 分类（对齐 transcript.py / statestore.base 注释）：
    - ``node`` 汇总（agent/input/output/tokens/cost/llm_steps/tool_steps）
    - ``llm_call`` 每次喂模型的完整 messages 原文 + tools + usage
    - ``tool_call`` 已放行工具的执行输入/结果（含 MCP server/read_only）
    - ``denied``   被权限 DENY 的工具（含 reason）
    ``node_id`` 省略 = 整 run 全量；``kind`` 可过滤。mock runner 无明细 → 空列表。
    """
    service = _service()
    await _run_for_tenant(run_id, ctx)  # 租户隔离（§9.2）
    store = await service.store_for(ctx.tenant_id)
    return await store.get_node_traces(run_id, node_id=node_id, kind=kind, limit=limit)


@app.post("/runs/{run_id}/approve")
async def approve(
    run_id: str,
    node_id: str | None = None,
    req: ApproveRequest | None = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> dict:
    """通过某审批节点。body ``{node_id}``（UI 契约）或 query ``?node_id=``（旧兼容）。

    JWT 模式下审批人身份取 token ``sub``（body ``by`` 仅 dev 模式生效）；
    审批人须在租户白名单内（§9.3 approvers，非白名单 403）。
    """
    await _run_for_tenant(run_id, ctx)
    rid = req.node_id if (req and req.node_id) else node_id
    if not rid:
        raise HTTPException(status_code=400, detail="需提供 node_id")
    by = (ctx.subject if (ctx.is_jwt and ctx.subject) else (req.by if req else "lead-engineer"))
    try:
        return await _service().approve(
            run_id, rid,
            approved=(req.approved if req else True),
            by=by,
            comment=(req.comment if req else ""),
            tenant_id=ctx.tenant_id,
        )
    except (ValueError, AssertionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ApprovalRaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApproverNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/runs/{run_id}/reject")
async def reject(
    run_id: str, req: RejectRequest, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """驳回某审批节点（UI 契约）。审批人校验同 approve。"""
    await _run_for_tenant(run_id, ctx)
    by = ctx.subject if (ctx.is_jwt and ctx.subject) else req.by
    try:
        return await _service().approve(
            run_id, req.node_id, approved=False, by=by, comment=req.comment,
            tenant_id=ctx.tenant_id,
        )
    except (ValueError, AssertionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ApprovalRaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApproverNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/runs/{run_id}/pause")
async def pause_run(run_id: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """暂停 run（§8.6）：当前节点跑完即暂停，checkpoint 保留；``/resume`` 恢复。"""
    await _run_for_tenant(run_id, ctx)
    try:
        await _service().pause_run(run_id, tenant_id=ctx.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id, "status": "pausing"}


@app.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """断点续跑（§4.4）：从 checkpoint + 原 snapshot 继续（queue 模式发布 resume 命令）。"""
    await _run_for_tenant(run_id, ctx)
    try:
        await _service().resume_run(run_id, tenant_id=ctx.tenant_id)
    except TenantQuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id, "status": "resumed"}


@app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, ctx: TenantContext = Depends(get_tenant_context)) -> dict:
    """停止进行中的 run（置 cancelled + 取消后台任务；queue 模式发布 stop 命令）。"""
    await _run_for_tenant(run_id, ctx)
    try:
        await _service().stop_run(run_id, tenant_id=ctx.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id}


@app.get("/audit")
async def audit(
    run_id: str | None = None,
    limit: int = 100,
    tenant_id: str | None = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[dict]:
    """审计日志查询（§9.5：tenant_id/tool_name/decision/run_id/node_id/input 脱敏/ts）。

    租户强制（§9.2）：JWT 模式下 tenant 取 token 派生值，请求参数 ``tenant_id`` 忽略；
    dev 模式（无 JWT）允许显式 tenant_id 便于联调。
    """
    effective_tenant = ctx.tenant_id if ctx.is_jwt else (tenant_id or ctx.tenant_id)
    service = _service()
    store = await service.store_for(ctx.tenant_id)
    return await store.get_audit_logs(
        tenant_id=effective_tenant, run_id=run_id, limit=limit
    )


# ── AgentSpec 配置 CRUD（SIP「Agent 配置」页：DB 驱动 agent 配置 + agent→MCP server 绑定）──
# 语义（对齐 docs plan §一）：description/system_prompt/schema 存 NULL=未覆盖→回退内置静态默认；
# mcp_server_ids 两态（v1.12.1 起）：NULL/[]（写入归一 NULL）=无 MCP server / [mid,…] = 精确子集。
# origin：builtin（seed，禁删，可编辑清空覆盖回退）| custom（POST 新建，可删）。
# 每次写操作后重建 _agent_config_resolver 并重接 mcp_manager.server_ids_for（运行时热生效）。
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_AGENT_ROLES = {"diagnose", "fix"}
_AGENT_STAGES_SET = {"detect", "diagnose", "fix", "verify", "deliver", "learn", "other"}
_BUILTIN_AGENT_NAMES = set(DIAGNOSE_AGENTS) | set(FIX_AGENTS)


class AgentConfigPayload(BaseModel):
    """AgentSpec 配置负载（POST 新建 custom / PUT 完整对象覆盖更新）。

    可覆盖字段：description/system_prompt/schema/mcp_server_ids/enabled/role/stage。
    ``""``/None 文本类 → 归 NULL（回退内置）；schema 不传/None → 回退内置（仅元数据/详情）。
    origin 不可经 API 修改（POST 一律 custom；PUT 保持行内 origin）。
    """

    name: str | None = None  # 仅 POST 必填；PUT 以路径 name 为准（本字段忽略）
    role: str | None = None
    stage: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    output_schema: dict | None = None  # 输出契约（仅元数据/详情）；不传/None → 回退内置
    mcp_server_ids: list[str] | None = None
    enabled: bool | None = None
    reasoning_enabled: bool | None = None  # Agent 级启用推理；None=不指定（update 保持原值 / create 默认关）


def _acfg_str(value: str | None) -> str | None:
    """'' / None / 纯空白 → None（未覆盖），否则去首尾空白。"""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_agent_role_stage(role: str | None, stage: str | None) -> None:
    if role is not None and role not in _AGENT_ROLES:
        raise HTTPException(status_code=400, detail="role 仅支持 diagnose 或 fix")
    if stage is not None and stage not in _AGENT_STAGES_SET:
        raise HTTPException(status_code=400, detail=f"stage 仅支持 {sorted(_AGENT_STAGES_SET)}")


async def _bound_servers(mcp_server_ids: list[str] | None, cs=None) -> list[dict]:
    """绑定 server 的 [{id, name, transport}]；无（NULL/空数组，两态语义）→ []。"""
    if not mcp_server_ids:
        return []
    mcp = cs.mcp if cs is not None else mcp_store
    out: list[dict] = []
    for mid in mcp_server_ids:
        row = await mcp.get(mid)
        out.append(
            {"id": row["id"], "name": row["name"], "transport": row["transport"]}
            if row is not None
            else {"id": mid, "name": mid, "transport": "?"}  # 残留引用（server 已删）
        )
    return out


async def _with_bound_servers(rows: list[dict], cs=None) -> list[dict]:
    resolver = AgentConfigResolver(rows)
    out: list[dict] = []
    for row in rows:
        eff = resolver.resolve(row["name"])
        out.append({
            **row,
            "effective_description": eff.description if eff else (row.get("description") or ""),
            "bound_servers": await _bound_servers(row.get("mcp_server_ids"), cs),
        })
    return out


@app.post("/agent-configs", status_code=201)
async def create_agent_config(
    payload: AgentConfigPayload, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """新建自定义 agent（origin=custom）。name 唯一（撞内置名 → 400）；custom 必填非空 system_prompt。"""
    name = _acfg_str(payload.name)
    if not name or not _AGENT_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="name 仅支持字母/数字/下划线/中划线（同时是 workflow node.agent 引用）")
    if name in _BUILTIN_AGENT_NAMES:
        raise HTTPException(status_code=400, detail=f"agent 名 {name!r} 是内置 agent，请直接编辑其配置")
    _validate_agent_role_stage(payload.role, payload.stage)
    if payload.role not in _AGENT_ROLES:
        raise HTTPException(status_code=400, detail="role 仅支持 diagnose 或 fix")
    system_prompt = _acfg_str(payload.system_prompt)
    if not system_prompt:
        raise HTTPException(status_code=400, detail="自定义 agent 必须提供非空 system_prompt")
    data = {
        "name": name,
        "origin": "custom",
        "role": payload.role,
        "stage": _acfg_str(payload.stage) or "other",
        "description": _acfg_str(payload.description),
        "system_prompt": system_prompt,
        "schema": payload.output_schema,
        "mcp_server_ids": payload.mcp_server_ids,
        "enabled": True if payload.enabled is None else payload.enabled,
        "reasoning_enabled": False if payload.reasoning_enabled is None else payload.reasoning_enabled,
    }
    cs = await _control_stores(ctx)
    try:
        await cs.agent_config.save(data)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f"agent 名 {name!r} 已存在") from None
    await _reload_agent_config_resolver(ctx.tenant_id)
    return {"name": name, "origin": "custom"}


@app.get("/agent-configs")
async def list_agent_configs(ctx: TenantContext = Depends(get_tenant_context)) -> list[dict]:
    """该租户的 AgentSpec 配置行（覆盖行；内置 15 靠静态回退，不占行）。

    含合并有效描述 + 绑定 server 名。"""
    cs = await _control_stores(ctx)
    rows = await cs.agent_config.list()
    return await _with_bound_servers(rows, cs)


@app.get("/agent-configs/{name}")
async def get_agent_config(
    name: str, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """单条：有效值已合并内置回退（供编辑弹窗回填）+ 绑定 server + stored（是否已覆盖）。

    内置 agent 无覆盖行时按静态注册表合成视图（v5.3：不再 seed 内置行）。"""
    cs = await _control_stores(ctx)
    row = await cs.agent_config.get(name)
    if row is None:
        if name not in _BUILTIN_AGENT_NAMES:
            raise HTTPException(status_code=404, detail=f"agent 配置不存在: {name!r}")
        # 内置无覆盖 → 合成行（stored 全 None = 未覆盖）
        row = {
            "name": name, "origin": "builtin", "role": None, "stage": None,
            "enabled": True, "reasoning_enabled": None, "mcp_server_ids": None,
            "description": None, "system_prompt": None, "schema": None,
        }
    eff = AgentConfigResolver([row]).resolve(name)
    return {
        "name": row["name"],
        "origin": row["origin"],
        "role": row["role"],
        "stage": row["stage"],
        "enabled": row["enabled"],
        "reasoning_enabled": row["reasoning_enabled"],
        "mcp_server_ids": row["mcp_server_ids"],
        "bound_servers": await _bound_servers(row.get("mcp_server_ids"), cs),
        "description": eff.description if eff else (row.get("description") or ""),
        "system_prompt": eff.system_prompt if eff else None,
        "schema": eff.schema if eff else {},
        "stored": {
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "schema": row["schema"],
        },
    }


@app.put("/agent-configs/{name}")
async def update_agent_config(
    name: str, payload: AgentConfigPayload, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """完整对象覆盖式更新（builtin/custom 均可）。文本清空/不传 → 归 NULL 回退内置；
    custom 不允许最终 system_prompt 为空。origin 保持不变。

    **内置 agent 无覆盖行时为 upsert**：v5.3 起不再 seed 内置行（GET 端点在无行时
    合成视图），若此处仍要求行存在，则「给未定制过的内置 agent 绑定 MCP server」
    这条路径根本走不通（PUT 恒 404）——而绑定 mcp_server_ids 正是控制面文档给出的
    用法。故对 builtin 名单内的名字，无行时按静态注册表合成基线并插入覆盖行。
    """
    cs = await _control_stores(ctx)
    existing = await cs.agent_config.get(name)
    if existing is None:
        if name not in _BUILTIN_AGENT_NAMES:
            raise HTTPException(status_code=404, detail=f"agent 配置不存在: {name!r}")
        # role/stage 是 NOT NULL 列，从静态注册表取真值（不能留 None）；
        # description/system_prompt/schema 留 NULL = 未覆盖 → 运行时回退静态默认。
        spec = get_agent_spec(name)
        existing = {
            "name": name, "origin": "builtin", "role": spec.role, "stage": spec.stage,
            "enabled": True, "reasoning_enabled": None, "mcp_server_ids": None,
        }
        await cs.agent_config.save({**existing, "description": None,
                                    "system_prompt": None, "schema": None})
    _validate_agent_role_stage(payload.role, payload.stage)
    role = payload.role if payload.role in _AGENT_ROLES else existing["role"]
    stage = _acfg_str(payload.stage) or existing["stage"]
    system_prompt = _acfg_str(payload.system_prompt)
    if existing["origin"] == "custom" and not system_prompt:
        raise HTTPException(status_code=400, detail="自定义 agent 必须提供非空 system_prompt")
    data = {
        "name": name,
        "origin": existing["origin"],
        "role": role,
        "stage": stage,
        "description": _acfg_str(payload.description),
        "system_prompt": system_prompt,
        "schema": payload.output_schema,
        "mcp_server_ids": payload.mcp_server_ids,
        "enabled": existing["enabled"] if payload.enabled is None else payload.enabled,
        "reasoning_enabled": (
            existing["reasoning_enabled"]
            if payload.reasoning_enabled is None
            else payload.reasoning_enabled
        ),
    }
    if not await cs.agent_config.update(name, data):
        raise HTTPException(status_code=404, detail=f"agent 配置不存在: {name!r}")
    await _reload_agent_config_resolver(ctx.tenant_id)
    return {"ok": True, "name": name}


@app.delete("/agent-configs/{name}")
async def delete_agent_config(
    name: str, ctx: TenantContext = Depends(get_tenant_context)
) -> dict:
    """删除自定义 agent；内置 agent 禁删（只能编辑/清空覆盖回退默认）。"""
    cs = await _control_stores(ctx)
    row = await cs.agent_config.get(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent 配置不存在: {name!r}")
    if row["origin"] == "builtin":
        raise HTTPException(status_code=400, detail="内置 agent 不可删除，请用编辑清空覆盖回退默认")
    if not await cs.agent_config.delete(name):
        raise HTTPException(status_code=404, detail=f"agent 配置不存在: {name!r}")
    await _reload_agent_config_resolver(ctx.tenant_id)
    return {"ok": True}


@app.get("/agents")
async def agents() -> list[dict]:
    """Agent 编队列表（DB AgentSpec 配置 + 内置静态默认的合并视图；与 /health 同级无鉴权）。

    来源 ``_effective_agent_resolver().all()``：内置 15（DB 覆盖或静态默认）∪ 自定义 agent。
    返回 [{name, description, tools, stage}]，tools 为该 agent 在 Tool Registry 中可见的函数
    工具名（L1 数据源，与 MCP 绑定无关），stage 为流水线阶段（detect/diagnose/fix/verify/
    deliver/learn），供前端舰队分组展示。store 为空（未 init）时 == 纯内置 15，形状/顺序与
    既有静态注册表一致。
    """
    return [
        {"name": spec.name, "description": spec.description,
         "tools": [t.name for t in tools_for_agent(spec.name)],
         "stage": spec.stage}
        for spec in _effective_agent_resolver().all()
    ]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "agentflow-control-plane"}
