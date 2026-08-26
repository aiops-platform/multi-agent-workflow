# -*- coding: utf-8 -*-
"""审批超时 Sweeper（design §8.9，控制面独立服务）。

周期扫描 WAITING_APPROVAL 审批：
1. 超时 → CAS 更新为 TIMED_OUT（仅当仍为 WAITING，终态不可逆 §8.3）
2. 节点置 rejected-canceled（output.approved=False, reason=timeout）
3. 发布 run.command resume（trigger=approval_timeout）→ Worker 继续
4. 通知审批方
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ..queue.base import Queue
from ..statestore.base import (
    APPROVAL_WAITING,
    APPROVAL_TIMED_OUT,
    StateStore,
)
from .notifier import ApprovalNotifier

log = logging.getLogger("agentflow.approval.sweeper")


class ApprovalSweeper:
    def __init__(
        self,
        store: StateStore,
        queue: Queue,
        notifier: ApprovalNotifier | None = None,
        *,
        interval: int = 60,
    ) -> None:
        self.store = store
        self.queue = queue
        self.notifier = notifier or ApprovalNotifier()
        self.interval = interval

    async def run_once(self) -> list[dict]:
        """扫描一轮，返回本轮超时并处理的审批。"""
        pending = await self.store.get_pending_approvals()
        now = datetime.now(timezone.utc)
        timed_out: list[dict] = []
        for ap in pending:
            timeout_at = ap.get("timeout_at")
            if not timeout_at:
                continue
            try:
                deadline = datetime.fromisoformat(timeout_at)
            except ValueError:
                continue
            if deadline > now:
                continue
            # CAS：仅当仍为 WAITING（终态不可逆）
            updated = await self.store.cas_update_approval(
                ap["approval_id"], APPROVAL_WAITING, APPROVAL_TIMED_OUT,
                comment="审批超时自动拒绝",
            )
            if not updated:
                continue  # 已被并发推进，跳过

            await self.store.update_node_status(
                ap["run_id"], ap["node_id"], "rejected-canceled",
                output={"approved": False, "reason": "timeout"},
            )
            await self.queue.publish(
                "run.command",
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
            except Exception as exc:  # noqa: BLE001 - 单轮失败不退出
                log.warning("sweeper 一轮失败: %s", exc)
            await asyncio.sleep(self.interval)
