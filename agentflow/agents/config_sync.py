"""跨进程配置热载：用**库内指纹**判断配置是否变过，变了才重建缓存。

**为什么不复用 API 侧那套代际计数器**（``app.py:_resolver_generation``）：那是
**进程内**变量。Worker 是独立进程，API 改了配置它根本看不见——跨进程失效只能靠
**库内可观测的信号**。

指纹取 ``(行数, MAX(updated_at))``：行数抓新增/删除，``updated_at`` 抓修改，两者合起
来覆盖增删改。（比单纯 ``MAX(updated_at)`` 强：删掉一条非最新的记录时 max 不变，但
行数会变。）

**行为**：每 ``interval`` 秒**至多**查一次库（默认 5s）。间隔内直接复用缓存——这让
「变更生效」有 ≤interval 的延迟，代价是每个节点执行只多两次极轻的 SELECT；相比
每个节点都重建（会反复建/拆 MCP client 与 stdio 连接）划算得多。
``interval=0`` 表示每次都查（测试用）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .agent_config import AgentConfigResolver

log = logging.getLogger("agentflow.config_sync")


def _signature(rows: list[dict[str, Any]]) -> tuple[int, str]:
    """一组配置行的指纹：行数 + 最大 updated_at（ISO 文本，字典序即时间序）。"""
    return (len(rows), max((r.get("updated_at") or "" for r in rows), default=""))


class TenantConfigSync:
    """按租户缓存 agent 配置解析器，并在指纹变化时重建（含 MCP client 重新对齐）。"""

    def __init__(self, router: Any, mcp_manager: Any = None, *, interval: float = 5.0) -> None:
        self._router = router
        self._mcp_manager = mcp_manager
        self._interval = max(0.0, interval)
        self._resolvers: dict[str, AgentConfigResolver] = {}
        self._fingerprints: dict[str, tuple] = {}
        self._checked_at: dict[str, float] = {}

    async def resolver(self, tenant_id: str | None) -> AgentConfigResolver:
        """取该租户的解析器；必要时先按指纹判定是否重建。

        runner 在取配置（本方法）之后才取 MCP client，故这里顺带做的
        ``mcp_manager.revalidate`` 能对当次节点执行生效。
        """
        key = tenant_id or "local"
        now = time.monotonic()
        cached = self._resolvers.get(key)

        if cached is not None and (now - self._checked_at.get(key, 0.0)) < self._interval:
            return cached  # 未到检查间隔 → 直接用缓存

        bundle = await self._router.get(key)
        agent_rows = await bundle.agent_config.list()
        mcp_rows = await bundle.mcp.list()
        fingerprint = (_signature(agent_rows), _signature(mcp_rows))
        self._checked_at[key] = now

        if cached is not None and fingerprint == self._fingerprints.get(key):
            return cached  # 配置没变 → 复用

        is_reload = cached is not None
        self._resolvers[key] = AgentConfigResolver(agent_rows)
        self._fingerprints[key] = fingerprint
        if self._mcp_manager is not None:
            # agent 绑定可能变（mcp_server_ids）+ server 本身可能增删改 —— 两处都要对齐。
            # 传**原始** tenant_id：mcp_manager._key 把 None 映射为 ""，与这里的 "local"
            # 是同一 store 但不同缓存键，归一化会让 revalidate 找错条目。
            await self._mcp_manager.revalidate(tenant_id)
        if is_reload:
            log.info("agent 配置已变更，重建解析器（tenant=%s）", key)
        return self._resolvers[key]
