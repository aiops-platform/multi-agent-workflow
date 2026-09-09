"""审批超时 Sweeper（design §8.9，控制面独立服务）。

周期扫描 WAITING_APPROVAL 审批：
1. 超时 → CAS 更新为 TIMED_OUT（仅当仍为 WAITING，终态不可逆 §8.3 + §8.3.2 时间谓词）
2. 节点置 rejected-canceled（output.approved=False, reason=timeout）
3. 发布 run.command resume（trigger=approval_timeout）→ Worker 继续
4. 通知审批方

多租户（design-v5.3 §5.3）：store 可为普通 StateStore（单库）或 TenantStoresRouter；
``tenants_provider``（管理库租户清单）驱动逐租户扫描各自租户库。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ..core.dag import REJECTED_CANCELED
from ..queue.base import Queue, topic_command
from ..statestore.base import APPROVAL_TIMED_OUT, APPROVAL_WAITING, StateStore
from ..statestore.router import store_resolver
from .notifier import ApprovalNotifier

log = logging.getLogger("agentflow.approval.sweeper")


class ApprovalSweeper:
    def __init__(
        self,
        store: StateStore | Any,
        queue: Queue,
        notifier: ApprovalNotifier | None = None,
        *,
        interval: int = 60,
        tenants_provider: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._stores = store_resolver(store)
        self.queue = queue
        self.notifier = notifier or ApprovalNotifier()
        self.interval = interval
        self._tenants_provider = tenants_provider

    async def _store(self, tenant_id: str | None) -> StateStore:
        return await self._stores.resolve(tenant_id or "local")

    async def run_once(self) -> list[dict]:
        """扫描一轮，返回本轮超时并处理的审批。

        tenants_provider 给出租户清单 → 逐租户扫描各自租户库；未提供（单库
        用法/测试）退化为单 store 扫描。
        """
        if self._tenants_provider is not None:
            timed_out: list[dict] = []
            for tid in await self._tenants_provider():
                store = await self._store(tid)
                timed_out.extend(await self._scan(store))
            return timed_out
        return await self._scan(await self._store(None))

    async def _scan(self, store: StateStore) -> list[dict]:
        pending = await store.get_pending_approvals()
        now = datetime.now(UTC)
        timed_out: list[dict] = []
        for ap in pending:
            timeout_at = ap.get("timeout_at")
            if not timeout_at:
                continue
            # PG 适配器把 TIMESTAMPTZ 列读回 datetime（非 str）；sqlite/memory 存的是 ISO str → 两者都兼容
            if isinstance(timeout_at, datetime):
                deadline = timeout_at
            else:
                try:
                    deadline = datetime.fromisoformat(timeout_at)
                except (TypeError, ValueError):
                    continue  # 无法解析的 deadline → 本轮跳过
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)  # naive → 视为 UTC，与 now 对齐
            if deadline > now:
                continue
            # CAS：仅当仍为 WAITING（终态不可逆 + §8.3.2 时间谓词：已过期才可置 TIMED_OUT）
            updated = await store.cas_update_approval(
                ap["approval_id"], APPROVAL_WAITING, APPROVAL_TIMED_OUT,
                comment="审批超时自动拒绝",
            )
            if not updated:
                continue  # 已被并发推进，跳过

            await store.update_node_status(
                ap["run_id"], ap["node_id"], REJECTED_CANCELED,
                output={"approved": False, "reason": "timeout"},
            )
            await self.queue.publish(
                topic_command(ap["tenant_id"]),
                key=ap["run_id"],
                message={
                    "type": "resume", "run_id": ap["run_id"],
                    "tenant_id": ap["tenant_id"], "trigger": "approval_timeout",
                },
            )
            await self.notifier.notify(
                kind="timeout", run_id=ap["run_id"], node_id=ap["node_id"],
                tenant_id=ap["tenant_id"], approvers=ap.get("approvers", []),
            )
            log.info("[%s] 审批 %s 超时 → TIMED_OUT，发布 resume", ap["run_id"], ap["node_id"])
            timed_out.append(ap)
        return timed_out

    async def run_forever(self) -> None:
        """后台常驻循环（控制面独立任务）。"""
        while True:
            try:
                await self.run_once()
            except Exception:
                log.warning("sweeper 一轮失败", exc_info=True)
            await asyncio.sleep(self.interval)
