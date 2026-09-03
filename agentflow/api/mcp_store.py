# -*- coding: utf-8 -*-
"""MCPStore：MCP server 配置持久化（供 SIP「MCP Server 配置」页面 CRUD + 运行时 MCPClientManager 读取）。

与运行期 StateStore / WorkflowStore 同库异表（复用 settings.state_db_path），aiosqlite 惰性连接
（幂等 connect），测试可脱离 lifespan 直接使用。逻辑列（transport/config/enable·disable_tools/
is_stateful/enabled）用 JSON/INT 落列，读取时反序列化为 Python 对象。

记录只描述 server 本身；**不存 agent 绑定**（原 ``agents`` 列已移除——agent 侧绑定后续以
agent 为主表建模，MCP server 不再承载绑定）。运行期 manager 将全部 enabled server 下发给 agent。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import postgres_dsn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,      -- 也是 MCPClient.name，须 ^[a-zA-Z0-9_-]+$
    transport     TEXT NOT NULL,             -- 'stdio' | 'http'
    config        TEXT NOT NULL,             -- JSON：stdio{command,args,env,cwd} / http{url,headers,timeout}
    is_stateful   INTEGER NOT NULL DEFAULT 1,-- stdio 强制 1；http 默认 0(stateless)
    enable_tools  TEXT,                      -- JSON list[str] | null
    disable_tools TEXT,                      -- JSON list[str] | null
    tools         TEXT,                      -- JSON list[dict] | null：最近一次 tools/list 快照（展示/选启用）
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

# 旧库迁移：早期建表无 tools 列 → connect 时按需 ALTER 补列；
# 曾有 agents 列（server 侧绑定）→ 已废弃，connect 时 DROP（绑定改到 agent 主表侧）。
_MIGRATION_COLUMNS = {
    "tools": "TEXT",
}
_DROP_COLUMNS = ("agents",)



def _dumps_nullable(obj: Any) -> str | None:
    """list[str] | None → JSON 文本或 None（enable_tools/disable_tools 列）。"""
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _to_row(data: dict[str, Any]) -> dict[str, Any]:
    """把写入方传入的业务 dict 规整为列值 dict（含时间戳），交给 INSERT/UPDATE。"""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "name": data["name"],
        "transport": data["transport"],
        "config": json.dumps(data["config"], ensure_ascii=False),
        "is_stateful": 1 if data.get("is_stateful") else 0,
        "enable_tools": _dumps_nullable(data.get("enable_tools")),
        "disable_tools": _dumps_nullable(data.get("disable_tools")),
        "tools": _dumps_nullable(data.get("tools")),
        "enabled": 1 if data.get("enabled", True) else 0,
        "updated_at": now,
    }


def _from_row(r: aiosqlite.Row) -> dict[str, Any]:
    """把 DB 行解析为对外业务 dict。"""
    return {
        "id": r["id"],
        "name": r["name"],
        "transport": r["transport"],
        "config": json.loads(r["config"]),
        "is_stateful": bool(r["is_stateful"]),
        "enable_tools": json.loads(r["enable_tools"]) if r["enable_tools"] is not None else None,
        "disable_tools": json.loads(r["disable_tools"]) if r["disable_tools"] is not None else None,
        "tools": json.loads(r["tools"]) if r["tools"] is not None else None,
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


class MCPStore:
    """MCP server 配置的轻量 CRUD 存储（aiosqlite，惰性连接）。"""

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
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """旧库结构迁移：补缺失列（_MIGRATION_COLUMNS）+ 丢弃废弃列（_DROP_COLUMNS）。

        CREATE TABLE IF NOT EXISTS 不会改既有表结构，故 connect 时按 PRAGMA 实际列比对：
        缺的加列、废弃的（如曾用于 server 侧绑定的 agents）DROP。
        """
        cur = await self._c.execute("PRAGMA table_info(mcp_servers)")
        existing = {row["name"] for row in await cur.fetchall()}
        for col, decl in _MIGRATION_COLUMNS.items():
            if col not in existing:
                await self._c.execute(f"ALTER TABLE mcp_servers ADD COLUMN {col} {decl}")
        for col in _DROP_COLUMNS:
            if col in existing:
                await self._c.execute(f"ALTER TABLE mcp_servers DROP COLUMN {col}")

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "MCPStore 未 connect()"
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def save(self, data: dict[str, Any]) -> str:
        """保存一条 MCP server 配置，返回 id。name 冲突抛 sqlite3.IntegrityError。"""
        await self.connect()
        mid = uuid.uuid4().hex[:12]
        col = _to_row(data)
        await self._c.execute(
            "INSERT INTO mcp_servers"
            "(id, name, transport, config, is_stateful, enable_tools, disable_tools,"
            " tools, enabled, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mid, col["name"], col["transport"], col["config"], col["is_stateful"],
             col["enable_tools"], col["disable_tools"], col["tools"],
             col["enabled"], col["updated_at"], col["updated_at"]),
        )
        await self._c.commit()
        return mid

    async def list(self) -> list[dict[str, Any]]:
        """全部记录（按创建时间倒序）。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT * FROM mcp_servers ORDER BY created_at DESC"
        )
        return [_from_row(r) for r in await cur.fetchall()]

    async def list_enabled(self) -> list[dict[str, Any]]:
        """enabled=1 的记录（运行时 MCPClientManager 加载用）。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT * FROM mcp_servers WHERE enabled=1 ORDER BY created_at DESC"
        )
        return [_from_row(r) for r in await cur.fetchall()]

    async def get(self, mid: str) -> dict[str, Any] | None:
        """读取完整记录。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT * FROM mcp_servers WHERE id=?", (mid,)
        )
        row = await cur.fetchone()
        return _from_row(row) if row is not None else None

    async def update(self, mid: str, data: dict[str, Any]) -> bool:
        """更新除 id/created_at 外的列，返回是否命中。"""
        await self.connect()
        col = _to_row(data)
        cur = await self._c.execute(
            "UPDATE mcp_servers SET name=?, transport=?, config=?, is_stateful=?,"
            " enable_tools=?, disable_tools=?, tools=?, enabled=?, updated_at=? WHERE id=?",
            (col["name"], col["transport"], col["config"], col["is_stateful"],
             col["enable_tools"], col["disable_tools"], col["tools"], col["enabled"],
             col["updated_at"], mid),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def update_tools(self, mid: str, tools: list[dict[str, Any]] | None) -> bool:
        """只刷新 tools 快照列（「重新拉取」回写用），返回是否命中。"""
        await self.connect()
        cur = await self._c.execute(
            "UPDATE mcp_servers SET tools=?, updated_at=? WHERE id=?",
            (json.dumps(tools, ensure_ascii=False) if tools is not None else None,
             datetime.now(timezone.utc).isoformat(), mid),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def delete(self, mid: str) -> bool:
        """删除，返回是否命中。"""
        await self.connect()
        cur = await self._c.execute("DELETE FROM mcp_servers WHERE id=?", (mid,))
        await self._c.commit()
        return cur.rowcount == 1


# ----------------------------------------------------------------------
# PostgreSQL 后端（state_store=postgres 时由 build_mcp_store 选择）
# ----------------------------------------------------------------------
# 列 schema 与 sqlite MCPStore 完全一致（JSON 存 TEXT、布尔存 INTEGER、时间戳存 ISO TEXT），
# 故 sqlite 的 _to_row/_from_row 可直接复用：插入参数序 / 读出 dict 形状两端相同。
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    transport     TEXT NOT NULL,
    config        TEXT NOT NULL,
    is_stateful   INTEGER NOT NULL DEFAULT 1,
    enable_tools  TEXT,
    disable_tools TEXT,
    tools         TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


class PgMCPStore:
    """MCPStore 的 PostgreSQL 实现（psycopg3 async，单连接，幂等 connect）。

    方法面 / 列结构 / 返回形状与 sqlite ``MCPStore`` 一致，供 API 层无感切换。
    name 唯一冲突：PG 的 UniqueViolation 收敛为 ``sqlite3.IntegrityError`` 抛出——
    app 端点本就捕获它映射为 400 中文「name 已存在」，避免跨库漏处理（另加注释说明原因）。
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
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """与 sqlite 端对齐：补缺失列 + 丢弃废弃列（曾用于 server 侧绑定的 agents）。"""
        cur = await self._c.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='mcp_servers'"
        )
        existing = {row["column_name"] for row in await cur.fetchall()}
        for col, decl in _MIGRATION_COLUMNS.items():
            if col not in existing:
                await self._c.execute(f"ALTER TABLE mcp_servers ADD COLUMN {col} {decl}")
        for col in _DROP_COLUMNS:
            if col in existing:
                await self._c.execute(f"ALTER TABLE mcp_servers DROP COLUMN {col}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self):
        assert self._conn is not None, "PgMCPStore 未 connect()"
        return self._conn

    async def save(self, data: dict[str, Any]) -> str:
        """保存一条配置，返回 id。name 重复 → sqlite3.IntegrityError（语义同 sqlite）。"""
        await self.connect()
        mid = uuid.uuid4().hex[:12]
        col = _to_row(data)
        try:
            await self._c.execute(
                "INSERT INTO mcp_servers"
                "(id, name, transport, config, is_stateful, enable_tools, disable_tools,"
                " tools, enabled, created_at, updated_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (mid, col["name"], col["transport"], col["config"], col["is_stateful"],
                 col["enable_tools"], col["disable_tools"], col["tools"],
                 col["enabled"], col["updated_at"], col["updated_at"]),
            )
        except Exception as exc:  # noqa: BLE001 —— 只认 unique violation，其余原样上抛
            from psycopg.errors import UniqueViolation

            if isinstance(exc, UniqueViolation):
                raise sqlite3.IntegrityError("UNIQUE constraint failed: mcp_servers.name") from exc
            raise
        await self._c.commit()
        return mid

    async def list(self) -> list[dict[str, Any]]:
        await self.connect()
        cur = await self._c.execute("SELECT * FROM mcp_servers ORDER BY created_at DESC")
        return [_from_row(r) for r in await cur.fetchall()]

    async def list_enabled(self) -> list[dict[str, Any]]:
        await self.connect()
        cur = await self._c.execute(
            "SELECT * FROM mcp_servers WHERE enabled=1 ORDER BY created_at DESC"
        )
        return [_from_row(r) for r in await cur.fetchall()]

    async def get(self, mid: str) -> dict[str, Any] | None:
        await self.connect()
        cur = await self._c.execute("SELECT * FROM mcp_servers WHERE id=%s", (mid,))
        row = await cur.fetchone()
        return _from_row(row) if row is not None else None

    async def update(self, mid: str, data: dict[str, Any]) -> bool:
        await self.connect()
        col = _to_row(data)
        cur = await self._c.execute(
            "UPDATE mcp_servers SET name=%s, transport=%s, config=%s, is_stateful=%s,"
            " enable_tools=%s, disable_tools=%s, tools=%s, enabled=%s, updated_at=%s WHERE id=%s",
            (col["name"], col["transport"], col["config"], col["is_stateful"],
             col["enable_tools"], col["disable_tools"], col["tools"], col["enabled"],
             col["updated_at"], mid),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def update_tools(self, mid: str, tools: list[dict[str, Any]] | None) -> bool:
        await self.connect()
        cur = await self._c.execute(
            "UPDATE mcp_servers SET tools=%s, updated_at=%s WHERE id=%s",
            (json.dumps(tools, ensure_ascii=False) if tools is not None else None,
             datetime.now(timezone.utc).isoformat(), mid),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def delete(self, mid: str) -> bool:
        await self.connect()
        cur = await self._c.execute("DELETE FROM mcp_servers WHERE id=%s", (mid,))
        await self._c.commit()
        return cur.rowcount == 1


def build_mcp_store(settings) -> MCPStore:
    """按 state_store 选择控制面 MCP 配置存储后端（对齐 statestore.build_state_store）。

    - sqlite/memory（本地 MVP）：沿用 settings.state_db_path 的 aiosqlite 实现；
    - postgres（生产 M6）：MCP 配置也落 PG，与运行期 runs/approvals 同库。
    """
    if settings.state_store == "postgres":
        return PgMCPStore(postgres_dsn(settings))
    return MCPStore(settings.state_db_path)
