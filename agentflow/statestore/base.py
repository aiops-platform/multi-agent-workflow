# -*- coding: utf-8 -*-
"""StateStore 接口 + 数据模型（design §8.8 完整表结构）。

本地 MVP 用 InMemory / SQLite；生产切换 PostgreSQL（M6）。所有表带
``tenant_id`` 分区键（§9 四层隔离的 DB 层）。

核心表：runs / nodes / approvals / node_attempts / workflow_snapshots
- nodes 存**节点级 checkpoint**（§8.4 幂等 + §4.4 Resume 的基础）
- approvals 支持 **CAS 更新**（§8.3.2，终态不可逆）
- node_attempts 记录 execution_id（§8.4 幂等）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# 审批状态机（§8.3.1，终态不可逆）
APPROVAL_WAITING = "WAITING_APPROVAL"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"
APPROVAL_TIMED_OUT = "TIMED_OUT"
APPROVAL_TERMINAL = {APPROVAL_APPROVED, APPROVAL_REJECTED, APPROVAL_TIMED_OUT}


class StateStore(ABC):
    # ---- workflow_snapshots（§8.5）----
    @abstractmethod
    async def save_snapshot(self, tenant_id: str, snapshot: dict) -> str:
        """保存 snapshot，返回 snapshot_id（= workflow_hash，同 hash 复用）。"""

    @abstractmethod
    async def get_snapshot(self, snapshot_id: str) -> dict | None:
        """读取 snapshot（Resume 用原版本，§8.5.2）。"""

    # ---- runs（§8.8）----
    @abstractmethod
    async def create_run(
        self, run_id: str, tenant_id: str, snapshot_id: str, inputs: dict
    ) -> None: ...

    @abstractmethod
    async def get_run(self, run_id: str) -> dict | None: ...

    @abstractmethod
    async def update_run(
        self, run_id: str, *, status: str | None = None, **fields: Any
    ) -> None: ...

    # ---- nodes：节点级 checkpoint（§8.4 / §4.4）----
    @abstractmethod
    async def put_node(
        self, run_id: str, tenant_id: str, node_id: str, cp: dict
    ) -> None: ...

    @abstractmethod
    async def get_nodes(self, run_id: str) -> dict[str, dict]: ...

    @abstractmethod
    async def update_node_status(
        self, run_id: str, node_id: str, status: str, output: dict | None = None
    ) -> None: ...

    # ---- approvals（§8.3）----
    @abstractmethod
    async def create_approval(
        self,
        run_id: str,
        node_id: str,
        tenant_id: str,
        *,
        params: dict,
        approvers: list[str],
        timeout_at: str,
    ) -> str: ...

    @abstractmethod
    async def get_pending_approvals(self) -> list[dict]: ...

    @abstractmethod
    async def get_approval(self, run_id: str, node_id: str) -> dict | None: ...

    @abstractmethod
    async def cas_update_approval(
        self,
        approval_id: str,
        from_status: str,
        to_status: str,
        *,
        by: str | None = None,
        comment: str | None = None,
    ) -> bool:
        """CAS 更新：仅当当前状态 == from_status 才更新为 to_status。返回是否成功。"""

    # ---- node_attempts：副作用幂等（§8.4）----
    @abstractmethod
    async def record_attempt(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        execution_id: str,
        status: str,
        *,
        output: Any = None,
        external_operation_id: str | None = None,
        error: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_attempt_by_execution_id(self, execution_id: str) -> dict | None: ...

    @abstractmethod
    async def get_succeeded_attempt(
        self, run_id: str, node_id: str, external_operation_id: str
    ) -> dict | None:
        """幂等复用：查同 run 同节点同 external_operation_id 的成功记录。"""
