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
from ..api.workflow_store import WorkflowStore, build_workflow_store
from ..config import Settings
from ..tenants import parse_db_ref
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

    async def aclose(self) -> None:
        for s in (self.state, self.workflow, self.mcp, self.agent_config):
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
            dsn = db_ref["dsn"]
            state = PostgresStateStore(dsn)
            workflow = build_workflow_store_at("postgres", dsn)
            mcp = build_mcp_store_at("postgres", dsn)
            agent_config = build_agent_config_store_at("postgres", dsn)
        elif backend == "memory":
            state = InMemoryStateStore()
            workflow = build_workflow_store_at("memory", None, settings=self._settings)
            mcp = build_mcp_store_at("memory", None, settings=self._settings)
            agent_config = build_agent_config_store_at("memory", None, settings=self._settings)
        else:
            path = db_ref["path"]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            state = SqliteStateStore(path)
            workflow = WorkflowStore(path)
            mcp = MCPStore(path)
            agent_config = AgentConfigStore(path)
        await state.connect()
        for s in (workflow, mcp, agent_config):
            await s.connect()
        return TenantStores(
            tenant_id=tenant_id, state=state, workflow=workflow,
            mcp=mcp, agent_config=agent_config,
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


def _default_db_ref(tenant_id: str, settings: Settings) -> dict:
    """未注册租户的回退 db_ref（§5.4：sqlite 每租户文件 / postgres 共享 DSN / memory 共享）。

    sqlite 路径跟随 ``settings.state_db_path``（测试 tmp 隔离；生产即配置的 data 目录）。"""
    from ..config import postgres_dsn

    if settings.state_store == "postgres":
        return {"backend": "postgres", "dsn": postgres_dsn(settings)}
    if settings.state_store == "memory":
        return {"backend": "memory"}
    return {
        "backend": "sqlite",
        "path": str(Path(settings.state_db_path).parent / "tenants" / f"{tenant_id}.db"),
    }


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
    """StateStore → 固定解析；TenantStoresRouter → 按租户解析（鸭子类型判定）。"""
    if isinstance(store_or_router, TenantStoresRouter):
        return _RouterStoreResolver(store_or_router)
    return _SingleStoreResolver(store_or_router)
