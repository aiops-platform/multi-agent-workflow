"""StateStoreRouter：租户 → 租户库路由（design-v5.3 §5.3，P4 的路由面）。

每租户一个 **TenantStores bundle**（运行期 StateStore + 三张控制面配置表 store），
按 db_ref 惰性构建 + LRU 缓存（淘汰即关闭连接）。租户库物理隔离：跨租户查询
物理不可能（§5.1）。

- db_ref 来源：管理库 ``tenants.db_ref_enc``（加密）→ 解密为
  ``{"backend": "sqlite"|"postgres", "path"/"dsn": ...}``；
- 管理库未注册的租户（dev/未开通）→ 按默认策略回退（sqlite: data/tenants/{t}.db；
  postgres: 共享 DSN）；
- ``store_resolver``：service/worker/sweeper 统一的解析入口——传**普通 StateStore**
  包装为固定解析（既有单库用法/测试零改动），传 Router 则按租户解析。
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api.agent_store import AgentConfigStore, build_agent_config_store
from ..api.management_store import decrypt_db_ref
from ..api.mcp_store import MCPStore, build_mcp_store
from ..api.ticket_store import TicketStore
from ..api.workflow_store import WorkflowStore, build_workflow_store
from ..config import Settings
from ..tenants import ensure_tenant_database, parse_db_ref
from .base import StateStore
from .memory import InMemoryStateStore
from .postgres import PostgresStateStore
from .sqlite import SqliteStateStore

log = logging.getLogger("agentflow.statestore.router")


@dataclass
class TenantStores:
    """一个租户的全部库连接（同一物理库的同库异表）。"""

    tenant_id: str
    state: StateStore
    workflow: WorkflowStore
    mcp: MCPStore
    agent_config: AgentConfigStore
    ticket: Any  # TicketStore | PgTicketStore

    async def aclose(self) -> None:
        for s in (self.state, self.workflow, self.mcp, self.agent_config, self.ticket):
            close = getattr(s, "close", None)
            if close is not None:
                await close()


class TenantStoresRouter:
    """tenant_id → TenantStores 惰性路由 + LRU（容量上限，淘汰即关闭）。"""

    def __init__(
        self,
        settings: Settings,
        management=None,
        *,
        max_size: int = 64,
    ) -> None:
        self._settings = settings
        self._management = management  # ManagementStore | None（无 → 默认策略回退）
        self._max_size = max_size
        self._cache: OrderedDict[str, TenantStores] = OrderedDict()

    async def get(self, tenant_id: str) -> TenantStores:
        if tenant_id in self._cache:
            self._cache.move_to_end(tenant_id)
            return self._cache[tenant_id]
        bundle = await self._build(tenant_id)
        self._cache[tenant_id] = bundle
        while len(self._cache) > self._max_size:
            _tid, evicted = self._cache.popitem(last=False)
            await evicted.aclose()
            log.info("Router LRU 淘汰租户库连接: %s", _tid)
        return bundle

    async def _build(self, tenant_id: str) -> TenantStores:
        db_ref = await self._resolve_ref(tenant_id)
        backend = db_ref.get("backend", "sqlite")
        if backend == "postgres":
            # 租户库可能还没建（首次访问 / 未走过 provision）→ 先补建再连。
            # 幂等：已存在时 ensure 直接返回 False，不产生额外 DDL。
            await ensure_tenant_database(db_ref, self._settings)
            dsn = db_ref["dsn"]
            state = PostgresStateStore(dsn)
            workflow = build_workflow_store_at("postgres", dsn)
            mcp = build_mcp_store_at("postgres", dsn)
            agent_config = build_agent_config_store_at("postgres", dsn)
            ticket = build_ticket_store_at("postgres", dsn)
        elif backend == "memory":
            state = InMemoryStateStore()
            workflow = build_workflow_store_at("memory", None, settings=self._settings)
            mcp = build_mcp_store_at("memory", None, settings=self._settings)
            agent_config = build_agent_config_store_at("memory", None, settings=self._settings)
            ticket = build_ticket_store_at("memory", None, settings=self._settings)
        else:
            path = db_ref["path"]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            state = SqliteStateStore(path)
            workflow = WorkflowStore(path)
            mcp = MCPStore(path)
            agent_config = AgentConfigStore(path)
            ticket = TicketStore(path)
        await state.connect()
        for s in (workflow, mcp, agent_config, ticket):
            await s.connect()

        # 新租户默认数据播种（seed/）：默认 workflow + MCP server + agent 绑定，
        # 让刚建好的库"开箱可用"。放这里而不是 `tenantctl provision`——`_build()` 本来就是
        # **ensure 语义**（它已经做 CREATE DATABASE + CREATE TABLE IF NOT EXISTS），
        # 且是所有入口（provision / migrate / API 启动 / Worker 装配 / 请求路径…）的
        # 唯一咽喉，将来多一个入口也不会漏。
        await _seed_if_enabled(self._settings, tenant_id, workflow, mcp, agent_config)

        return TenantStores(
            tenant_id=tenant_id, state=state, workflow=workflow,
            mcp=mcp, agent_config=agent_config, ticket=ticket,
        )

    async def _resolve_ref(self, tenant_id: str) -> dict:
        if self._management is not None:
            row = await self._management.get_tenant(tenant_id)
            if row is not None:
                return parse_db_ref(decrypt_db_ref(row["db_ref_enc"], self._settings))
        return _default_db_ref(tenant_id, self._settings)

    async def aclose(self) -> None:
        for _tid, bundle in self._cache.items():
            await bundle.aclose()
        self._cache.clear()


async def _seed_if_enabled(
    settings: Settings, tenant_id: str, workflow, mcp, agent_config
) -> None:
    """播种默认数据（空表才播、绝不覆盖）；失败**只记日志**。

    抽成函数是为了让 `_build()` 与 `build_tenant_stores_at_dsn()` **共用同一份语义**
    ——两条路径都是「拿到一个可用的租户库」，播种行为必须一致，否则 `--dsn` 部署
    会静默拿到一个空库（有库、有表、没有 workflow 与 MCP 绑定 → run 跑完全空转）。
    """
    if not settings.seed_defaults:
        return
    from ..seed import seed_defaults  # 惰性：与上面的 store import 同风格

    try:
        counts = await seed_defaults(workflow, mcp, agent_config, settings=settings)
        if any(counts.values()):
            log.info(
                "租户 %s 播种默认数据：workflows=%d servers=%d agents=%d"
                "（种子非真源，见 agentflow/seed/README.md）",
                tenant_id, counts["workflows"], counts["servers"], counts["agents"],
            )
    except Exception:
        # 播种失败**绝不能**拖垮建库/连接——那会让该租户的所有请求 500。
        # seed 内部已按表兜底，这里是最后一道。
        log.exception("租户 %s 播种默认数据失败（已跳过，不影响连接）", tenant_id)


async def build_tenant_stores_at_dsn(
    dsn: str, tenant_id: str, settings: Settings, *, state=None
) -> TenantStores:
    """按**给定 DSN 直接**构造租户 bundle（不经管理库 / db_ref）。

    K8s 容器形态用（`worker --dsn`）：容器内能连到的库地址与 provision 时写进管理库
    的 db_ref **网络不同**（provision 在宿主上跑，记的是 localhost），Router 那条路
    在容器里连不上。

    但 bundle 的**形状必须与 Router 一致**：`workflows` / `mcp_servers` / `agent_configs`
    **都在这个库里**，装配 node_runner 要读后两者。只换 StateStore 会把 MCP 绑定与
    agent 配置一起丢掉 —— 旧 `--dsn` 分支正是如此（`agents` 零工具、run 全空转）。

    ``state`` 可传入已建好的 StateStore（调用方通常已经连过一次，避免重复连接）。
    """
    if state is None:
        state = PostgresStateStore(dsn)
        await state.connect()
    workflow = build_workflow_store_at("postgres", dsn)
    mcp = build_mcp_store_at("postgres", dsn)
    agent_config = build_agent_config_store_at("postgres", dsn)
    ticket = build_ticket_store_at("postgres", dsn)
    for s in (workflow, mcp, agent_config, ticket):
        await s.connect()
    await _seed_if_enabled(settings, tenant_id, workflow, mcp, agent_config)
    return TenantStores(
        tenant_id=tenant_id, state=state, workflow=workflow,
        mcp=mcp, agent_config=agent_config, ticket=ticket,
    )


def _default_db_ref(tenant_id: str, settings: Settings) -> dict:
    """未注册租户的回退 db_ref。

    ``memory`` 是本模块独有的回退档（``tenants._default_db_ref`` 没有它，那是
    给 bootstrap/provision 用的持久后端选择）；sqlite / postgres 一律转交共享实现，
    保证「未注册租户的回退」与「provision 时写入的 db_ref」**用的是同一套规则** ——
    早期两处各写一份，postgres 分支都返回共享 DSN，隔离就是从这里漏掉的。
    """
    if settings.state_store == "memory":
        return {"backend": "memory"}
    from ..tenants import _default_db_ref as _shared

    return _shared(tenant_id, settings)


def build_workflow_store_at(backend: str, dsn: str | None, settings: Settings | None = None):
    if backend == "postgres":
        from ..api.workflow_store import PgWorkflowStore

        return PgWorkflowStore(dsn)
    assert settings is not None
    return build_workflow_store(settings)


def build_mcp_store_at(backend: str, dsn: str | None, settings: Settings | None = None):
    if backend == "postgres":
        from ..api.mcp_store import PgMCPStore

        return PgMCPStore(dsn)
    assert settings is not None
    return build_mcp_store(settings)


def build_agent_config_store_at(backend: str, dsn: str | None, settings: Settings | None = None):
    if backend == "postgres":
        from ..api.agent_store import PgAgentConfigStore

        return PgAgentConfigStore(dsn)
    assert settings is not None
    return build_agent_config_store(settings)


def build_ticket_store_at(backend: str, dsn: str | None, settings: Settings | None = None):
    if backend == "postgres":
        from ..api.ticket_store import PgTicketStore

        return PgTicketStore(dsn)
    assert settings is not None
    from ..api.ticket_store import build_ticket_store

    return build_ticket_store(settings)


# ----------------------------------------------------------------------
# service/worker/sweeper 的统一解析入口
# ----------------------------------------------------------------------
class _SingleStoreResolver:
    """固定单库（既有 RunService(store) 用法/测试零改动）。"""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def resolve(self, tenant_id: str) -> StateStore:
        return self._store

    async def aclose(self) -> None:
        close = getattr(self._store, "close", None)
        if close is not None:
            await close()


class _RouterStoreResolver:
    """Router → 租户库的运行期 StateStore。"""

    def __init__(self, router: TenantStoresRouter) -> None:
        self._router = router

    async def resolve(self, tenant_id: str) -> StateStore:
        bundle = await self._router.get(tenant_id)
        return bundle.state

    async def aclose(self) -> None:
        await self._router.aclose()


def store_resolver(store_or_router: Any) -> Any:
    """StateStore → 固定解析；TenantStoresRouter → 按租户解析；已有 resolver 原样返回。"""
    if isinstance(store_or_router, TenantStoresRouter):
        return _RouterStoreResolver(store_or_router)
    if hasattr(store_or_router, "resolve"):
        return store_or_router  # 已是 resolver（WorkerPool 复用）
    return _SingleStoreResolver(store_or_router)
