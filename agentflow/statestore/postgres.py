# -*- coding: utf-8 -*-
"""PostgreSQL StateStore（design §8.8 完整表结构，M6 生产适配器）。

与 sqlite 实现同接口（runs/nodes/approvals/node_attempts/workflow_snapshots/audit_logs，
均带 tenant_id 分区键）。用 psycopg3 异步驱动。
"""
from __future__ import annotations

import json
from typing import Any

from .base import APPROVAL_WAITING, StateStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    workflow_hash TEXT NOT NULL,
    workflow_yaml TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    workflow_snapshot_id TEXT NOT NULL,
    status              TEXT NOT NULL,
    inputs              JSONB,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS nodes (
    run_id    TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    status    TEXT NOT NULL,
    output    JSONB,
    cp        JSONB NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    approvers   JSONB,
    params      JSONB,
    timeout_at  TIMESTAMPTZ,
    approved_by TEXT,
    comment     TEXT,
    UNIQUE (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
    execution_id          TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL,
    node_id               TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    status                TEXT NOT NULL,
    output                JSONB,
    external_operation_id TEXT,
    error                 TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    decision     TEXT NOT NULL,
    run_id       TEXT,
    node_id      TEXT,
    input_masked TEXT,
    actor        TEXT,
    ts           TIMESTAMPTZ DEFAULT now()
);
"""


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class PostgresStateStore(StateStore):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None

    async def connect(self) -> None:
        from psycopg import AsyncConnection

        self._conn = await AsyncConnection.connect(self._dsn)
        await self._conn.execute(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self):
        assert self._conn is not None, "PostgresStateStore 未 connect()"
        return self._conn

    # ---- workflow_snapshots ----
    async def save_snapshot(self, tenant_id: str, snapshot: dict) -> str:
        sid = snapshot["workflow_hash"]
        await self._c.execute(
            "INSERT INTO workflow_snapshots(snapshot_id, tenant_id, workflow_hash, workflow_yaml)"
            " VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (sid, tenant_id, snapshot["workflow_hash"], snapshot["workflow_yaml"]),
        )
        await self._c.commit()
        return sid

    async def get_snapshot(self, snapshot_id: str) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM workflow_snapshots WHERE snapshot_id=%s", (snapshot_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ---- runs ----
    async def create_run(self, run_id, tenant_id, snapshot_id, inputs) -> None:
        await self._c.execute(
            "INSERT INTO runs(run_id, tenant_id, workflow_snapshot_id, status, inputs)"
            " VALUES(%s,%s,%s,'running',%s::jsonb)",
            (run_id, tenant_id, snapshot_id, _j(inputs)),
        )
        await self._c.commit()

    async def get_run(self, run_id) -> dict | None:
        cur = await self._c.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("inputs"):
            d["inputs"] = json.loads(d["inputs"])
        return d

    async def update_run(self, run_id, *, status=None, **fields) -> None:
        cols, vals = [], []
        if status is not None:
            cols.append("status=%s"); vals.append(status)
        for k, v in fields.items():
            cols.append(f"{k}=%s"); vals.append(v)
        vals.append(run_id)
        await self._c.execute(
            f"UPDATE runs SET {', '.join(cols)}, updated_at=now() WHERE run_id=%s", vals
        )
        await self._c.commit()

    # ---- nodes ----
    async def put_node(self, run_id, tenant_id, node_id, cp) -> None:
        await self._c.execute(
            "INSERT INTO nodes(run_id, node_id, tenant_id, status, output, cp)"
            " VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
            " ON CONFLICT (run_id, node_id) DO UPDATE SET status=EXCLUDED.status,"
            " output=EXCLUDED.output, cp=EXCLUDED.cp",
            (run_id, node_id, tenant_id, cp.get("status", "pending"), _j(cp.get("output")), _j(cp)),
        )
        await self._c.commit()

    async def get_nodes(self, run_id) -> dict[str, dict]:
        cur = await self._c.execute(
            "SELECT node_id, cp FROM nodes WHERE run_id=%s ORDER BY node_id", (run_id,)
        )
        rows = await cur.fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    async def update_node_status(self, run_id, node_id, status, output=None) -> None:
        await self._c.execute(
            "UPDATE nodes SET status=%s, output=%s::jsonb WHERE run_id=%s AND node_id=%s",
            (status, _j(output) if output is not None else None, run_id, node_id),
        )
        await self._c.commit()

    # ---- approvals ----
    async def create_approval(self, run_id, node_id, tenant_id, *, params, approvers, timeout_at) -> str:
        aid = f"ap_{run_id}_{node_id}"
        await self._c.execute(
            "INSERT INTO approvals(approval_id, run_id, node_id, tenant_id, status, approvers, params, timeout_at)"
            " VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::timestamptz)",
            (aid, run_id, node_id, tenant_id, APPROVAL_WAITING, _j(approvers), _j(params), timeout_at),
        )
        await self._c.commit()
        return aid

    async def get_pending_approvals(self) -> list[dict]:
        cur = await self._c.execute(
            "SELECT * FROM approvals WHERE status=%s", (APPROVAL_WAITING,)
        )
        rows = await cur.fetchall()
        return [self._approval_dict(r) for r in rows]

    @staticmethod
    def _approval_dict(row) -> dict:
        d = dict(row)
        d["approvers"] = json.loads(d["approvers"]) if d.get("approvers") else []
        d["params"] = json.loads(d["params"]) if d.get("params") else {}
        return d

    async def get_approval(self, run_id, node_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM approvals WHERE run_id=%s AND node_id=%s", (run_id, node_id)
        )
        row = await cur.fetchone()
        return self._approval_dict(row) if row else None

    async def cas_update_approval(self, approval_id, from_status, to_status, *, by=None, comment=None) -> bool:
        cur = await self._c.execute(
            "UPDATE approvals SET status=%s, approved_by=%s, comment=%s"
            " WHERE approval_id=%s AND status=%s",
            (to_status, by, comment, approval_id, from_status),
        )
        await self._c.commit()
        return cur.rowcount == 1

    # ---- node_attempts ----
    async def record_attempt(self, run_id, node_id, attempt, execution_id, status, *, output=None, external_operation_id=None, error=None) -> None:
        await self._c.execute(
            "INSERT INTO node_attempts(execution_id, run_id, node_id, attempt, status, output, external_operation_id, error)"
            " VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
            (execution_id, run_id, node_id, attempt, status, _j(output) if output is not None else None,
             external_operation_id, error),
        )
        await self._c.commit()

    async def get_attempt_by_execution_id(self, execution_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM node_attempts WHERE execution_id=%s", (execution_id,)
        )
        row = await cur.fetchone()
        return self._attempt_dict(row) if row else None

    async def get_succeeded_attempt(self, run_id, node_id, external_operation_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM node_attempts WHERE run_id=%s AND node_id=%s AND external_operation_id=%s AND status='succeeded'",
            (run_id, node_id, external_operation_id),
        )
        row = await cur.fetchone()
        return self._attempt_dict(row) if row else None

    @staticmethod
    def _attempt_dict(row) -> dict:
        d = dict(row)
        d["output"] = json.loads(d["output"]) if d.get("output") else None
        return d

    # ---- audit_logs ----
    async def append_audit(self, tenant_id, *, tool_name, decision, run_id, node_id, input_masked=None, actor=None) -> None:
        await self._c.execute(
            "INSERT INTO audit_logs(tenant_id, tool_name, decision, run_id, node_id, input_masked, actor)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id, tool_name, decision, run_id, node_id, input_masked, actor),
        )
        await self._c.commit()

    async def get_audit_logs(self, *, tenant_id=None, run_id=None, limit=100) -> list[dict]:
        sql = "SELECT * FROM audit_logs WHERE TRUE"
        params: list = []
        if tenant_id:
            sql += " AND tenant_id=%s"; params.append(tenant_id)
        if run_id:
            sql += " AND run_id=%s"; params.append(run_id)
        sql += " ORDER BY id DESC LIMIT %s"; params.append(limit)
        cur = await self._c.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
