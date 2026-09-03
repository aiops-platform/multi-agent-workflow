# -*- coding: utf-8 -*-
"""WorkflowStore：流程配置页面的 workflow 持久化（供 SIP「Workflow Studio」页面使用）。

与运行期 StateStore（runs/nodes/approvals）分离：本表只存「用户保存的 workflow 定义」
（name + YAML 文本），供前端流程配置页做 CRUD 复用。SQLite 惰性连接（幂等 connect），
测试可脱离 lifespan 直接使用。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import postgres_dsn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    yaml       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class WorkflowStore:
    """workflow 定义的轻量 CRUD 存储（aiosqlite，惰性连接）。"""

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
        assert self._conn is not None, "WorkflowStore 未 connect()"
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def save(self, name: str, yaml_text: str) -> str:
        """保存一条 workflow，返回 id。"""
        await self.connect()
        wid = uuid.uuid4().hex[:12]
        await self._c.execute(
            "INSERT INTO workflows(id, name, yaml, created_at) VALUES(?,?,?,?)",
            (wid, name, yaml_text, datetime.now(timezone.utc).isoformat()),
        )
        await self._c.commit()
        return wid

    async def list(self) -> list[dict[str, Any]]:
        """按创建时间倒序列出 [{id, name, created_at}]。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT id, name, created_at FROM workflows ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"]} for r in rows]

    async def get(self, wid: str) -> dict[str, Any] | None:
        """读取完整记录 {id, name, yaml}。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT id, name, yaml FROM workflows WHERE id=?", (wid,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "name": row["name"], "yaml": row["yaml"]}

    async def update(self, wid: str, name: str, yaml_text: str) -> bool:
        """更新 name/yaml，返回是否命中。"""
        await self.connect()
        cur = await self._c.execute(
            "UPDATE workflows SET name=?, yaml=? WHERE id=?", (name, yaml_text, wid)
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def delete(self, wid: str) -> bool:
        """删除，返回是否命中。"""
        await self.connect()
        cur = await self._c.execute("DELETE FROM workflows WHERE id=?", (wid,))
        await self._c.commit()
        return cur.rowcount == 1


# ----------------------------------------------------------------------
# PostgreSQL 后端（state_store=postgres 时由 build_workflow_store 选择）
# ----------------------------------------------------------------------
# 列 schema 与 sqlite WorkflowStore 完全一致（文本列、时间戳存 ISO TEXT），
# 方法面 / 返回形状两端相同，供 API 层无感切换。name 无 UNIQUE 约束（与 sqlite
# 一致：重名在应用层 pre-check，不做 DB 唯一约束），故无需跨库归一化异常。
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    yaml       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class PgWorkflowStore:
    """WorkflowStore 的 PostgreSQL 实现（psycopg3 async，单连接，幂等 connect）。

    方法面 / 返回形状与 sqlite ``WorkflowStore`` 一致，供 API 层无感切换。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None

    async def connect(self) -> None:
        """幂等连接；首次调用时建表（IF NOT EXISTS，与 docker/init 幂等对齐）。"""
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
        assert self._conn is not None, "PgWorkflowStore 未 connect()"
        return self._conn

    async def save(self, name: str, yaml_text: str) -> str:
        """保存一条 workflow，返回 id。"""
        await self.connect()
        wid = uuid.uuid4().hex[:12]
        await self._c.execute(
            "INSERT INTO workflows(id, name, yaml, created_at) VALUES(%s,%s,%s,%s)",
            (wid, name, yaml_text, datetime.now(timezone.utc).isoformat()),
        )
        await self._c.commit()
        return wid

    async def list(self) -> list[dict[str, Any]]:
        """按创建时间倒序列出 [{id, name, created_at}]。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT id, name, created_at FROM workflows ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"]} for r in rows]

    async def get(self, wid: str) -> dict[str, Any] | None:
        """读取完整记录 {id, name, yaml}。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT id, name, yaml FROM workflows WHERE id=%s", (wid,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "name": row["name"], "yaml": row["yaml"]}

    async def update(self, wid: str, name: str, yaml_text: str) -> bool:
        """更新 name/yaml，返回是否命中。"""
        await self.connect()
        cur = await self._c.execute(
            "UPDATE workflows SET name=%s, yaml=%s WHERE id=%s", (name, yaml_text, wid)
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def delete(self, wid: str) -> bool:
        """删除，返回是否命中。"""
        await self.connect()
        cur = await self._c.execute("DELETE FROM workflows WHERE id=%s", (wid,))
        await self._c.commit()
        return cur.rowcount == 1


def build_workflow_store(settings) -> WorkflowStore:
    """按 state_store 选择控制面 workflow 配置存储后端（对齐 mcp_store.build_mcp_store）。

    - sqlite/memory（本地 MVP）：沿用 settings.state_db_path 的 aiosqlite 实现；
    - postgres（生产 M6）：workflow 配置也落 PG，与运行期 runs/approvals 同库。
    """
    if settings.state_store == "postgres":
        return PgWorkflowStore(postgres_dsn(settings))
    return WorkflowStore(settings.state_db_path)
