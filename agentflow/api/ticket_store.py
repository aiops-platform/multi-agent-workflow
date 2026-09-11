"""TicketStore：运维工单的持久化（供 SIP「Ticket Inbox」页面使用）。

**为什么需要它**：控制面此前在 run 维度是「一次性」的 —— ``ticket`` 只是 ``POST /run``
body 的一个字段，被拍平成 run 的 ``inputs`` 存一次，此后再无任何 API 能读回它。
于是「这条 run 来自哪个工单」查不回来，前端也没法列出待处理的工单。

本表补上这一层：工单独立落库，记录它发起过哪些 run（``run_ids``），
使「工单 → run → 诊断/修复结果」这条链可查。

**租户隔离**：所有方法都带 ``tenant_id`` 并强制过滤（跨租户查不到，不是靠调用方自觉）。
注意 ``WorkflowStore`` 没有 tenant 列（历史遗留，workflow 定义目前在控制面库全局共享），
本表**刻意不跟随**那个模式 —— 工单天然是租户数据。

与 ``WorkflowStore`` 同款形态：SQLite（aiosqlite，惰性幂等 connect）+
PostgreSQL（psycopg3 async）两实现，方法面与返回形状一致，供 API 层无感切换。

**JSON 列一律用 TEXT**（不用 jsonb）：一是两端解码一致，二是避开 psycopg 对 jsonb
自动解析成 dict、而 sqlite 返回 str 的不对称（本仓已在别处踩过这个坑）。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import postgres_dsn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    number     TEXT,
    title      TEXT NOT NULL,
    service    TEXT,
    namespace  TEXT,
    severity   TEXT,
    status     TEXT NOT NULL,
    inputs     TEXT NOT NULL,
    run_ids    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_tenant ON tickets(tenant_id, created_at DESC);
"""

# 工单状态。与 run 状态分开 —— 一个工单可以发起多次 run，工单自身的进度
# 是它所有 run 的汇总，不等于任何单个 run 的状态。
TICKET_NEW = "new"
TICKET_RUNNING = "running"
TICKET_RESOLVED = "resolved"
TICKET_FAILED = "failed"

_COLS = (
    "id, tenant_id, number, title, service, namespace, severity,"
    " status, inputs, run_ids, created_at, updated_at"
)


def _row_to_ticket(row: Any) -> dict[str, Any]:
    """行 → dict；统一反序列化两个 JSON TEXT 列。"""
    d = dict(row)
    d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
    d["run_ids"] = json.loads(d["run_ids"]) if d.get("run_ids") else []
    return d


class TicketStore:
    """工单的轻量 CRUD 存储（aiosqlite，惰性连接）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """幂等连接；首次调用时建表。"""
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "TicketStore 未 connect()"
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def create(
        self,
        tenant_id: str,
        *,
        title: str,
        inputs: dict,
        number: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str = TICKET_NEW,
    ) -> str:
        """建工单，返回 id（12 位 hex）。"""
        await self.connect()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        await self._c.execute(
            f"INSERT INTO tickets({_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid, tenant_id, number, title, service, namespace, severity,
                status, json.dumps(inputs, ensure_ascii=False), "[]", now, now,
            ),
        )
        await self._c.commit()
        return tid

    async def list(
        self, tenant_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """按创建时间倒序列出该租户的工单。"""
        await self.connect()
        where = ["tenant_id = ?"]
        vals: list[Any] = [tenant_id]
        if status is not None:
            where.append("status = ?")
            vals.append(status)
        vals.extend([limit, offset])
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            vals,
        )
        return [_row_to_ticket(r) for r in await cur.fetchall()]

    async def get(self, tenant_id: str, tid: str) -> dict[str, Any] | None:
        """取单条（**强制租户过滤** —— 跨租户返回 None，不是 403，避免泄漏存在性）。"""
        await self.connect()
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE id=? AND tenant_id=?", (tid, tenant_id)
        )
        row = await cur.fetchone()
        return _row_to_ticket(row) if row else None

    async def attach_run(self, tenant_id: str, tid: str, run_id: str) -> bool:
        """把一次 run 挂到工单上（幂等：重复挂同一个 run_id 不重复追加）。"""
        ticket = await self.get(tenant_id, tid)
        if ticket is None:
            return False
        if run_id in ticket["run_ids"]:
            return True
        run_ids = [*ticket["run_ids"], run_id]
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET run_ids=?, updated_at=? WHERE id=? AND tenant_id=?",
            (json.dumps(run_ids), datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def set_status(self, tenant_id: str, tid: str, status: str) -> bool:
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
            (status, datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1


# ----------------------------------------------------------------------
# PostgreSQL 后端（state_store=postgres 时由 build_ticket_store 选择）
# ----------------------------------------------------------------------
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    number     TEXT,
    title      TEXT NOT NULL,
    service    TEXT,
    namespace  TEXT,
    severity   TEXT,
    status     TEXT NOT NULL,
    inputs     TEXT NOT NULL,
    run_ids    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_tenant ON tickets(tenant_id, created_at DESC);
"""


class PgTicketStore:
    """TicketStore 的 PostgreSQL 实现（psycopg3 async，单连接，幂等 connect）。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        self._conn = await AsyncConnection.connect(self._dsn)
        self._conn.row_factory = dict_row
        await self._conn.execute(_PG_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self):
        assert self._conn is not None, "PgTicketStore 未 connect()"
        return self._conn

    async def create(
        self,
        tenant_id: str,
        *,
        title: str,
        inputs: dict,
        number: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str = TICKET_NEW,
    ) -> str:
        await self.connect()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        await self._c.execute(
            f"INSERT INTO tickets({_COLS}) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tid, tenant_id, number, title, service, namespace, severity,
                status, json.dumps(inputs, ensure_ascii=False), "[]", now, now,
            ),
        )
        await self._c.commit()
        return tid

    async def list(
        self, tenant_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        await self.connect()
        where = ["tenant_id = %s"]
        vals: list[Any] = [tenant_id]
        if status is not None:
            where.append("status = %s")
            vals.append(status)
        vals.extend([limit, offset])
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            vals,
        )
        return [_row_to_ticket(r) for r in await cur.fetchall()]

    async def get(self, tenant_id: str, tid: str) -> dict[str, Any] | None:
        await self.connect()
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE id=%s AND tenant_id=%s", (tid, tenant_id)
        )
        row = await cur.fetchone()
        return _row_to_ticket(row) if row else None

    async def attach_run(self, tenant_id: str, tid: str, run_id: str) -> bool:
        ticket = await self.get(tenant_id, tid)
        if ticket is None:
            return False
        if run_id in ticket["run_ids"]:
            return True
        run_ids = [*ticket["run_ids"], run_id]
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET run_ids=%s, updated_at=%s WHERE id=%s AND tenant_id=%s",
            (json.dumps(run_ids), datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def set_status(self, tenant_id: str, tid: str, status: str) -> bool:
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET status=%s, updated_at=%s WHERE id=%s AND tenant_id=%s",
            (status, datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1


def build_ticket_store(settings):
    """按 state_store 选择控制面 ticket 存储后端（对齐 build_workflow_store）。"""
    if settings.state_store == "postgres":
        return PgTicketStore(postgres_dsn(settings))
    return TicketStore(settings.state_db_path)
