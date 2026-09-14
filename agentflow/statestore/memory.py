"""InMemory StateStore（本地测试 / 单进程 MVP）。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .base import ACTIVE_RUN_STATUSES, APPROVAL_WAITING, StateStore, approval_time_guard


def _parse_deadline(value: Any) -> datetime | None:
    """timeout_at 兼容 datetime / ISO 字符串；naive 视为 UTC（对齐 sweeper 规则）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        deadline = value
    else:
        try:
            deadline = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._snapshots: dict[str, dict] = {}
        self._runs: dict[str, dict] = {}
        self._nodes: dict[str, dict[str, dict]] = {}
        self._approvals: dict[str, dict] = {}
        self._attempts: dict[str, dict] = {}
        self._attempt_seq: int = 0
        self._audit: list[dict] = []
        self._traces: list[dict] = []
        self._trace_seq: int = 0

    # ---- workflow_snapshots ----
    async def save_snapshot(self, tenant_id: str, snapshot: dict) -> str:
        sid = snapshot["workflow_hash"]
        self._snapshots.setdefault(sid, {**snapshot, "tenant_id": tenant_id})
        return sid

    async def get_snapshot(self, snapshot_id: str) -> dict | None:
        return self._snapshots.get(snapshot_id)

    # ---- runs ----
    async def create_run(self, run_id, tenant_id, snapshot_id, inputs) -> None:
        now = datetime.now(UTC).isoformat()
        self._runs[run_id] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "workflow_snapshot_id": snapshot_id,
            "status": "running",
            "inputs": inputs,
            # 与 sqlite/PG 对齐（那两边的列有 DEFAULT，这里得自己写）
            "created_at": now,
            "updated_at": now,
        }

    async def get_run(self, run_id) -> dict | None:
        return self._runs.get(run_id)

    async def update_run(self, run_id, *, status=None, **fields) -> None:
        run = self._runs.setdefault(run_id, {})
        if status is not None:
            run["status"] = status
        run.update(fields)
        run["updated_at"] = datetime.now(UTC).isoformat()

    async def list_runs(
        self, tenant_id, *, status=None, run_ids=None, limit=50, offset=0
    ) -> list[dict]:
        rows = [r for r in self._runs.values() if r.get("tenant_id") == tenant_id]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        if run_ids is not None:
            wanted = set(run_ids)
            rows = [r for r in rows if r.get("run_id") in wanted]
        rows.sort(key=lambda r: (r.get("created_at") or "", r["run_id"]), reverse=True)
        return [dict(r) for r in rows[offset : offset + limit]]

    async def list_attempts(self, run_id) -> list[dict]:
        rows = [a for a in self._attempts.values() if a.get("run_id") == run_id]
        rows.sort(key=lambda a: (a.get("node_id") or "", a.get("attempt") or 0))
        return [dict(a) for a in rows]

    async def count_active_runs(self, tenant_id) -> int:
        return sum(
            1
            for r in self._runs.values()
            if r.get("tenant_id") == tenant_id and r.get("status") in ACTIVE_RUN_STATUSES
        )

    async def cas_update_run_status(self, run_id, from_status, to_status) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.get("status") != from_status:
            return False
        run["status"] = to_status
        return True

    # ---- nodes ----
    async def put_node(self, run_id, tenant_id, node_id, cp) -> None:
        self._nodes.setdefault(run_id, {})[node_id] = {**cp, "tenant_id": tenant_id}

    async def get_nodes(self, run_id) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._nodes.get(run_id, {}).items()}

    async def update_node_status(self, run_id, node_id, status, output=None) -> None:
        st = self._nodes.setdefault(run_id, {}).setdefault(node_id, {})
        st["status"] = status
        if output is not None:
            st["output"] = output

    # ---- approvals ----
    async def create_approval(self, run_id, node_id, tenant_id, *, params, approvers, timeout_at) -> str:
        aid = f"ap_{run_id}_{node_id}"
        self._approvals[aid] = {
            "approval_id": aid,
            "run_id": run_id,
            "node_id": node_id,
            "tenant_id": tenant_id,
            "status": APPROVAL_WAITING,
            "params": params,
            "approvers": approvers,
            "timeout_at": timeout_at,
        }
        return aid

    async def get_pending_approvals(self) -> list[dict]:
        return [a for a in self._approvals.values() if a["status"] == APPROVAL_WAITING]

    async def get_approval(self, run_id, node_id) -> dict | None:
        return self._approvals.get(f"ap_{run_id}_{node_id}")

    async def cas_update_approval(self, approval_id, from_status, to_status, *, by=None, comment=None) -> bool:
        a = self._approvals.get(approval_id)
        if not a or a["status"] != from_status:
            return False
        # §8.3.2 时间原子判定（与 sqlite/postgres SQL 谓词同语义）
        guard = approval_time_guard(from_status, to_status)
        if guard is not None:
            deadline = _parse_deadline(a.get("timeout_at"))
            now = datetime.now(UTC)
            if guard == "expired":
                if deadline is None or deadline > now:
                    return False
            elif deadline is not None and deadline <= now:
                return False
        a["status"] = to_status
        if by is not None:
            a["approved_by"] = by
        if comment is not None:
            a["comment"] = comment
        return True

    # ---- node_attempts ----
    async def record_attempt(self, run_id, node_id, attempt, execution_id, status, *, output=None, external_operation_id=None, error=None) -> None:
        self._attempt_seq += 1
        self._attempts[execution_id] = {
            "execution_id": execution_id,
            "run_id": run_id,
            "node_id": node_id,
            "attempt": attempt,
            "status": status,
            "output": output,
            "external_operation_id": external_operation_id,
            "error": error,
        }

    async def get_attempt_by_execution_id(self, execution_id) -> dict | None:
        return self._attempts.get(execution_id)

    async def get_succeeded_attempt(self, run_id, node_id, external_operation_id) -> dict | None:
        for a in self._attempts.values():
            if (
                a["run_id"] == run_id
                and a["node_id"] == node_id
                and a["external_operation_id"] == external_operation_id
                and a["status"] == "succeeded"
            ):
                return a
        return None

    # ---- audit_logs ----
    async def append_audit(self, tenant_id, *, tool_name, decision, run_id, node_id, input_masked=None, actor=None) -> None:
        self._audit.append({
            "tenant_id": tenant_id, "tool_name": tool_name, "decision": decision,
            "run_id": run_id, "node_id": node_id, "input_masked": input_masked,
            "actor": actor,
            "ts": datetime.now(UTC).isoformat(),
        })

    async def get_audit_logs(self, *, tenant_id=None, run_id=None, limit=100) -> list[dict]:
        out = [
            a for a in self._audit
            if (tenant_id is None or a["tenant_id"] == tenant_id)
            and (run_id is None or a["run_id"] == run_id)
        ]
        return out[-limit:]

    # ---- node_traces：节点级事件流水明细（先删后插 → retry/resume 只留末次成功 attempt 全量流水）----
    async def replace_node_traces(self, run_id, node_id, tenant_id, *, rows) -> None:
        self._traces = [
            t for t in self._traces
            if not (t["run_id"] == run_id and t["node_id"] == node_id)
        ]
        ts = datetime.now(UTC).isoformat()
        for i, r in enumerate(rows):
            self._traces.append({
                "id": self._trace_seq,
                "seq": i,  # 与 sqlite/PG 一致：整节点替换后按 0 起重算
                "run_id": run_id,
                "node_id": node_id,
                "tenant_id": tenant_id,
                "kind": r.get("kind", ""),
                "name": r.get("name"),
                "payload": r.get("payload", {}),
                "ts": ts,
            })
            self._trace_seq += 1

    async def get_node_traces(self, run_id, node_id=None, *, kind=None, limit=500) -> list[dict]:
        out = [
            t for t in self._traces
            if t["run_id"] == run_id
            and (node_id is None or t["node_id"] == node_id)
            and (kind is None or t["kind"] == kind)
        ]
        # 与 sqlite/PG 的 ORDER BY node_id, id 一致
        out.sort(key=lambda t: (t["node_id"], t["id"]))
        return list(out[:limit])
