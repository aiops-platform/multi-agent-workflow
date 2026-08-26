# -*- coding: utf-8 -*-
"""InMemory StateStore（本地测试 / 单进程 MVP）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .base import APPROVAL_WAITING, StateStore


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._snapshots: dict[str, dict] = {}
        self._runs: dict[str, dict] = {}
        self._nodes: dict[str, dict[str, dict]] = {}
        self._approvals: dict[str, dict] = {}
        self._attempts: dict[str, dict] = {}
        self._attempt_seq: int = 0
        self._audit: list[dict] = []

    # ---- workflow_snapshots ----
    async def save_snapshot(self, tenant_id: str, snapshot: dict) -> str:
        sid = snapshot["workflow_hash"]
        self._snapshots.setdefault(sid, {**snapshot, "tenant_id": tenant_id})
        return sid

    async def get_snapshot(self, snapshot_id: str) -> dict | None:
        return self._snapshots.get(snapshot_id)

    # ---- runs ----
    async def create_run(self, run_id, tenant_id, snapshot_id, inputs) -> None:
        self._runs[run_id] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "workflow_snapshot_id": snapshot_id,
            "status": "running",
            "inputs": inputs,
        }

    async def get_run(self, run_id) -> dict | None:
        return self._runs.get(run_id)

    async def update_run(self, run_id, *, status=None, **fields) -> None:
        run = self._runs.setdefault(run_id, {})
        if status is not None:
            run["status"] = status
        run.update(fields)

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
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    async def get_audit_logs(self, *, tenant_id=None, run_id=None, limit=100) -> list[dict]:
        out = [
            a for a in self._audit
            if (tenant_id is None or a["tenant_id"] == tenant_id)
            and (run_id is None or a["run_id"] == run_id)
        ]
        return out[-limit:]
