# -*- coding: utf-8 -*-
"""SQLite StateStore（本地 MVP 默认后端；表结构对齐 design §8.8）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .base import APPROVAL_WAITING, StateStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    workflow_hash TEXT NOT NULL,
    workflow_yaml TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    workflow_snapshot_id TEXT NOT NULL,
    status              TEXT NOT NULL,
    inputs              TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS nodes (
    run_id    TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    status    TEXT NOT NULL,
    output    TEXT,
    cp        TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    approvers   TEXT,
    params      TEXT,
    timeout_at  TEXT,
    approved_by TEXT,
    comment     TEXT,
    UNIQUE (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
    execution_id         TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    node_id              TEXT NOT NULL,
    attempt              INTEGER NOT NULL,
    status               TEXT NOT NULL,
    output               TEXT,
    external_operation_id TEXT,
    error                TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    decision      TEXT NOT NULL,
    run_id        TEXT,
    node_id       TEXT,
    input_masked  TEXT,
    actor         TEXT,
    ts            TEXT
);
"""


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class SqliteStateStore(StateStore):
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        if db_path not in (":memory:",) and str(db_path) != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "StateStore 未 connect()，先 await store.connect()"
        return self._conn

    # ---- workflow_snapshots ----
    async def save_snapshot(self, tenant_id: str, snapshot: dict) -> str:
        sid = snapshot["workflow_hash"]
        await self._c.execute(
            "INSERT OR IGNORE INTO workflow_snapshots"
            "(snapshot_id, tenant_id, workflow_hash, workflow_yaml) VALUES(?,?,?,?)",
            (sid, tenant_id, snapshot["workflow_hash"], snapshot["workflow_yaml"]),
        )
        await self._c.commit()
        return sid

    async def get_snapshot(self, snapshot_id: str) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM workflow_snapshots WHERE snapshot_id=?", (snapshot_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ---- runs ----
    async def create_run(self, run_id, tenant_id, snapshot_id, inputs) -> None:
        await self._c.execute(
            "INSERT INTO runs(run_id, tenant_id, workflow_snapshot_id, status, inputs)"
            " VALUES(?,?,?,?,?)",
            (run_id, tenant_id, snapshot_id, "running", _j(inputs)),
        )
        await self._c.commit()

    async def get_run(self, run_id) -> dict | None:
        cur = await self._c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
        return d

    async def update_run(self, run_id, *, status=None, **fields) -> None:
        cols, vals = [], []
        if status is not None:
            cols.append("status=?"); vals.append(status)
        for k, v in fields.items():
            cols.append(f"{k}=?"); vals.append(v)
        vals.append(run_id)
        await self._c.execute(
            f"UPDATE runs SET {', '.join(cols)}, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
            vals,
        )
        await self._c.commit()

    # ---- nodes ----
    async def put_node(self, run_id, tenant_id, node_id, cp) -> None:
        await self._c.execute(
            "INSERT OR REPLACE INTO nodes(run_id, node_id, tenant_id, status, output, cp)"
            " VALUES(?,?,?,?,?,?)",
            (
                run_id, node_id, tenant_id,
                cp.get("status", "pending"),
                _j(cp.get("output")),
                _j(cp),
            ),
        )
        await self._c.commit()

    async def get_nodes(self, run_id) -> dict[str, dict]:
        cur = await self._c.execute(
            "SELECT node_id, cp FROM nodes WHERE run_id=? ORDER BY node_id", (run_id,)
        )
        rows = await cur.fetchall()
        return {r["node_id"]: json.loads(r["cp"]) for r in rows}

    async def update_node_status(self, run_id, node_id, status, output=None) -> None:
        await self._c.execute(
            "UPDATE nodes SET status=?, output=? WHERE run_id=? AND node_id=?",
            (status, _j(output) if output is not None else None, run_id, node_id),
        )
        await self._c.commit()

    # ---- approvals ----
    async def create_approval(self, run_id, node_id, tenant_id, *, params, approvers, timeout_at) -> str:
        aid = f"ap_{run_id}_{node_id}"
        await self._c.execute(
            "INSERT INTO approvals(approval_id, run_id, node_id, tenant_id, status, approvers, params, timeout_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (aid, run_id, node_id, tenant_id, APPROVAL_WAITING, _j(approvers), _j(params), timeout_at),
        )
        await self._c.commit()
        return aid

    async def get_pending_approvals(self) -> list[dict]:
        cur = await self._c.execute(
            "SELECT * FROM approvals WHERE status=?", (APPROVAL_WAITING,)
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["approvers"] = json.loads(d["approvers"] or "[]")
            d["params"] = json.loads(d["params"] or "{}")
            out.append(d)
        return out

    async def get_approval(self, run_id, node_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM approvals WHERE run_id=? AND node_id=?", (run_id, node_id)
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["approvers"] = json.loads(d["approvers"] or "[]")
        d["params"] = json.loads(d["params"] or "{}")
        return d

    async def cas_update_approval(self, approval_id, from_status, to_status, *, by=None, comment=None) -> bool:
        cur = await self._c.execute(
            "UPDATE approvals SET status=?, approved_by=?, comment=?"
            " WHERE approval_id=? AND status=?",
            (to_status, by, comment, approval_id, from_status),
        )
        await self._c.commit()
        return cur.rowcount == 1

    # ---- node_attempts ----
    async def record_attempt(self, run_id, node_id, attempt, execution_id, status, *, output=None, external_operation_id=None, error=None) -> None:
        await self._c.execute(
            "INSERT INTO node_attempts(execution_id, run_id, node_id, attempt, status, output, external_operation_id, error)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (execution_id, run_id, node_id, attempt, status, _j(output) if output is not None else None, external_operation_id, error),
        )
        await self._c.commit()

    async def get_attempt_by_execution_id(self, execution_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM node_attempts WHERE execution_id=?", (execution_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["output"] = json.loads(d["output"]) if d.get("output") else None
        return d

    async def get_succeeded_attempt(self, run_id, node_id, external_operation_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM node_attempts WHERE run_id=? AND node_id=? AND external_operation_id=? AND status='succeeded'",
            (run_id, node_id, external_operation_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["output"] = json.loads(d["output"]) if d.get("output") else None
        return d

    # ---- audit_logs ----
    async def append_audit(self, tenant_id, *, tool_name, decision, run_id, node_id, input_masked=None, actor=None) -> None:
        await self._c.execute(
            "INSERT INTO audit_logs(tenant_id, tool_name, decision, run_id, node_id, input_masked, actor, ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (tenant_id, tool_name, decision, run_id, node_id, input_masked, actor,
             __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()),
        )
        await self._c.commit()

    async def get_audit_logs(self, *, tenant_id=None, run_id=None, limit=100) -> list[dict]:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params: list = []
        if tenant_id:
            sql += " AND tenant_id=?"; params.append(tenant_id)
        if run_id:
            sql += " AND run_id=?"; params.append(run_id)
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        cur = await self._c.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
