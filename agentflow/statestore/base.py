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

# 占用租户并发配额的 run 状态（§9.3 max_concurrent_runs）：未到终态的都算占用
ACTIVE_RUN_STATUSES = ("running", "queued", "waiting_approval", "paused")


def approval_time_guard(from_status: str, to_status: str) -> str | None:
    """CAS 更新审批时需要的时间谓词种类（§8.3.2 ``AND timeout_at > NOW()``）。

    - ``'unexpired'``：WAITING → APPROVED/REJECTED 仅允许在超时窗口内（审批过期即不可批，
      堵住「已超时但 sweeper 尚未扫到」窗口内 approve 仍能成功的竞态）。
    - ``'expired'``：WAITING → TIMED_OUT 仅允许在超时后（sweeper 语义）。
    - ``None``：其余转换不加时间谓词（状态谓词已足够，如终态不可逆）。
    """
    if from_status != APPROVAL_WAITING or to_status == APPROVAL_WAITING:
        return None
    if to_status == APPROVAL_TIMED_OUT:
        return "expired"
    return "unexpired"


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

    @abstractmethod
    async def count_active_runs(self, tenant_id: str) -> int:
        """租户未终态 run 数（§9.3 max_concurrent_runs 配额判定用）。"""

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
        """CAS 更新：仅当当前状态 == from_status 才更新为 to_status。返回是否成功。

        除状态谓词外，还须按 :func:`approval_time_guard` 施加时间原子判定
        （approve/reject 仅未超时可批；TIMED_OUT 仅超时后可置，§8.3.2）。
        """

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

    # ---- audit_logs（§8.8 审计表 / §9.5 审计字段）----
    @abstractmethod
    async def append_audit(
        self,
        tenant_id: str,
        *,
        tool_name: str,
        decision: str,
        run_id: str,
        node_id: str,
        input_masked: str | None = None,
        actor: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_audit_logs(
        self, *, tenant_id: str | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[dict]: ...

    # ---- node_traces：节点级事件流水明细（LLM/工具/MCP/汇总，全量不脱敏）----
    @abstractmethod
    async def replace_node_traces(
        self,
        run_id: str,
        node_id: str,
        tenant_id: str,
        *,
        rows: list[dict],
    ) -> None:
        """整节点替换事件流水（先删后插）。rows 每项含 kind/name/payload；
        seq 由实现按枚举重算、ts 实现自填。仅节点最终成功后调一次 →
        retry/resume 只留最后一次成功 attempt 的完整流水。"""

    @abstractmethod
    async def get_node_traces(
        self,
        run_id: str,
        node_id: str | None = None,
        *,
        kind: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """按 (node_id, id) 升序读流水；kind 可过滤（'node'/'llm_call'/'tool_call'/'denied'）。"""
