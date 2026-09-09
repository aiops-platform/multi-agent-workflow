"""PostgreSQL StateStore（design §8.8 完整表结构，M6 生产适配器）。

与 sqlite 实现同接口（runs/nodes/approvals/node_attempts/workflow_snapshots/audit_logs，
均带 tenant_id 分区键）。用 psycopg3 异步驱动。
"""
from __future__ import annotations

import json
from typing import Any

from .base import (
    ACTIVE_RUN_STATUSES,
    APPROVAL_WAITING,
    StateStore,
    approval_time_guard,
)

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
CREATE TABLE IF NOT EXISTS node_traces (
    id        BIGSERIAL PRIMARY KEY,
    run_id    TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    name      TEXT,
    payload   JSONB NOT NULL,
    ts        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_node_traces_run ON node_traces(run_id, node_id);
"""


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _row_to_dict(cur, row) -> dict:
    """psycopg 默认 tuple row → dict，按游标列名映射。

    连接未开 ``dict_row``（见 ``connect``），fetch 回来的是 tuple；``dict(row)``
    会把每个元素当 key/value 对强转，遇字符串即 ValueError。jsonb 列 psycopg
    已自动解析为 dict/list，调用方勿再 ``json.loads``。
    """
    return dict(zip([c.name for c in cur.description], row))


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
        return _row_to_dict(cur, row) if row else None

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
        # inputs 为 jsonb 列，psycopg 已解析为 dict，直接返回（勿再 json.loads）
        return _row_to_dict(cur, row)

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

    async def count_active_runs(self, tenant_id) -> int:
        placeholders = ",".join("%s" for _ in ACTIVE_RUN_STATUSES)
        cur = await self._c.execute(
            f"SELECT COUNT(*) FROM runs WHERE tenant_id=%s AND status IN ({placeholders})",
            (tenant_id, *ACTIVE_RUN_STATUSES),
        )
        row = await cur.fetchone()
        return int(row[0])

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
        # cp 为 jsonb 列，已解析为 dict（原 json.loads 会对 dict 抛 TypeError）
        return {r[0]: r[1] for r in rows}

    async def update_node_status(self, run_id, node_id, status, output=None) -> None:
        # 与 sqlite 同语义：cp 必须与 status/output 列同步（Resume 读 cp），
        # 否则 from_checkpoint 拿到陈旧 checkpoint（审批超时卡死教训）。
        cur = await self._c.execute(
            "SELECT cp FROM nodes WHERE run_id=%s AND node_id=%s", (run_id, node_id)
        )
        row = await cur.fetchone()
        cp = dict(row[0]) if row and row[0] else {}
        cp["status"] = status
        if output is not None:
            cp["output"] = output
        await self._c.execute(
            "UPDATE nodes SET status=%s, output=%s::jsonb, cp=%s::jsonb"
            " WHERE run_id=%s AND node_id=%s",
            (status, _j(output) if output is not None else None, _j(cp), run_id, node_id),
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
        return [self._approval_dict(_row_to_dict(cur, r)) for r in rows]

    @staticmethod
    def _approval_dict(d: dict) -> dict:
        # approvers/params 为 jsonb 列，psycopg 已解析（list/dict）；兜底空值
        d["approvers"] = d["approvers"] if isinstance(d.get("approvers"), list) else []
        d["params"] = d["params"] if isinstance(d.get("params"), dict) else {}
        return d

    async def get_approval(self, run_id, node_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM approvals WHERE run_id=%s AND node_id=%s", (run_id, node_id)
        )
        row = await cur.fetchone()
        return self._approval_dict(_row_to_dict(cur, row)) if row else None

    async def cas_update_approval(self, approval_id, from_status, to_status, *, by=None, comment=None) -> bool:
        sql = (
            "UPDATE approvals SET status=%s, approved_by=%s, comment=%s"
            " WHERE approval_id=%s AND status=%s"
        )
        # §8.3.2 时间原子判定：timeout_at 为 TIMESTAMPTZ，直接用服务器时钟 NOW()
        guard = approval_time_guard(from_status, to_status)
        if guard == "expired":
            sql += " AND timeout_at IS NOT NULL AND timeout_at <= NOW()"
        elif guard == "unexpired":
            sql += " AND (timeout_at IS NULL OR timeout_at > NOW())"
        cur = await self._c.execute(sql, (to_status, by, comment, approval_id, from_status))
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
        return self._attempt_dict(_row_to_dict(cur, row)) if row else None

    async def get_succeeded_attempt(self, run_id, node_id, external_operation_id) -> dict | None:
        cur = await self._c.execute(
            "SELECT * FROM node_attempts WHERE run_id=%s AND node_id=%s AND external_operation_id=%s AND status='succeeded'",
            (run_id, node_id, external_operation_id),
        )
        row = await cur.fetchone()
        return self._attempt_dict(_row_to_dict(cur, row)) if row else None

    @staticmethod
    def _attempt_dict(d: dict) -> dict:
        # output 为 jsonb 列，psycopg 已解析（dict/None），勿再 json.loads
        d["output"] = d.get("output")
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
        return [_row_to_dict(cur, r) for r in rows]

    # ---- node_traces：节点级事件流水明细（先删后插 → retry/resume 只留末次成功 attempt 全量流水）----
    async def replace_node_traces(self, run_id, node_id, tenant_id, *, rows) -> None:
        await self._c.execute(
            "DELETE FROM node_traces WHERE run_id=%s AND node_id=%s", (run_id, node_id)
        )
        if rows:
            # ts 依赖列 DEFAULT now()；seq 按传入顺序从 0 重算。
            # 注意：psycopg3 AsyncConnection 无 executemany（那是 cursor 的方法）→ 逐行 execute。
            async with self._conn.cursor() as cur:
                for i, r in enumerate(rows):
                    await cur.execute(
                        "INSERT INTO node_traces(run_id, node_id, tenant_id, seq, kind, name, payload)"
                        " VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)",
                        (run_id, node_id, tenant_id, i, r.get("kind", ""), r.get("name"),
                         _j(r.get("payload", {}))),
                    )
        await self._c.commit()

    async def get_node_traces(self, run_id, node_id=None, *, kind=None, limit=500) -> list[dict]:
        sql = "SELECT * FROM node_traces WHERE run_id=%s"
        params: list = [run_id]
        if node_id:
            sql += " AND node_id=%s"; params.append(node_id)
        if kind:
            sql += " AND kind=%s"; params.append(kind)
        sql += " ORDER BY node_id, id LIMIT %s"; params.append(limit)
        cur = await self._c.execute(sql, params)
        rows = await cur.fetchall()
        # payload 为 jsonb 列，psycopg 已解析为 dict/list（勿再 json.loads）
        return [_row_to_dict(cur, r) for r in rows]
