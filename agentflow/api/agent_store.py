# -*- coding: utf-8 -*-
"""AgentConfigStore：AgentSpec 配置持久化（供 SIP「Agent 配置」页 CRUD + 运行时 AgentNodeRunner 读取）。

与运行期 StateStore / 控制面 workflows / mcp_servers 同库异表（复用 settings.state_db_path），
aiosqlite 惰性连接（幂等 connect），测试可脱离 lifespan 直接使用。

表记录 agent 的**可配置 AgentSpec**（name/role/stage/description + 可覆盖的
system_prompt/schema + enabled + MCP 绑定）。语义（对齐 plan §一）：
- ``description / system_prompt / schema`` 存 NULL 表示「未覆盖 → 运行时回退内置静态默认」；
  写入非空即覆盖。schema 覆盖仅为元数据/详情层（``extract_json`` 不做强校验，对齐现状）。
- 内置默认**物化**（见 ``seed_builtin_agent_configs``）：空表 seed / 对既有内置行回填时，
  description/system_prompt/schema_json 一并落静态默认，让 DB 成为内置 agent 的可见配置快照。
  一旦物化，改代码里 SYSTEM_PROMPTS/AGENT_SCHEMAS 不再自动生效；清空该字段保存（→NULL）即回退
  内置，并随下次启动重新固化为当前默认。
- ``mcp_server_ids`` 两态（v1.12.1 起）：NULL（或 ``[]``，写入归一为 NULL）=**无 MCP server**；
  ``[mid,…]``=精确子集。「没配置就没有 server」——不再有「未配置=全量 enabled」的默认。
- ``origin``：'builtin'（代码内置 seed，禁止删除）| 'custom'（页面新增，可删）。
  内置 agent 的「重置/清空覆盖」= 把上面可空列清成 NULL。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import postgres_dsn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_configs (
    name            TEXT PRIMARY KEY,             -- agent 名（= workflow node.agent / SYSTEM_PROMPTS key）
    origin          TEXT NOT NULL DEFAULT 'builtin',   -- 'builtin' | 'custom'
    role            TEXT NOT NULL,                -- 'diagnose' | 'fix'
    stage           TEXT NOT NULL DEFAULT 'other',     -- detect/diagnose/fix/verify/deliver/learn
    description     TEXT,                         -- NULL→回退 AGENT_DESCRIPTIONS
    system_prompt   TEXT,                         -- NULL→回退 SYSTEM_PROMPTS
    schema_json     TEXT,                         -- JSON dict | NULL→回退 AGENT_SCHEMAS（仅元数据/详情）
    mcp_server_ids  TEXT,                         -- JSON list[str] | NULL；NULL=无 server（两态，见模块 docstring）
    enabled         INTEGER NOT NULL DEFAULT 1,
    reasoning_enabled INTEGER NOT NULL DEFAULT 0, -- 0=常规模型；1=该 agent 用 thinking 推理模型（CoT 落 trace）
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

# 结构迁移：老库（reasoning_enabled 列加入前建的 agent_configs 表）缺列时由 connect() 补上。
# sqlite 走 _migrate()（PRAGMA 查列）；PG 走 connect() 里同款 information_schema 受保护 ALTER。
_MIGRATION_COLUMNS: dict[str, str] = {
    "reasoning_enabled": "INTEGER NOT NULL DEFAULT 0",
}
_DROP_COLUMNS: tuple[str, ...] = ()


def _opt_str(value: Any) -> str | None:
    """'' / None / 纯空白 → None（表示未覆盖，回退内置）；其余字符串化并去首尾空白。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value).strip() or None


def _dumps(obj: Any) -> str | None:
    """Python 对象（dict/list）→ JSON 文本；None/'' → None。schema/mcp_server_ids 列用。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj.strip() or None  # 前端可能直接发 JSON 文本；不二次 dumps
    return json.dumps(obj, ensure_ascii=False)


def _opt_ids(value: Any) -> str | None:
    """``mcp_server_ids`` 列写入归一：None / 空数组 / '' → NULL（无 server）；非空数组→ JSON。

    两态语义下 ``[]``（明确不绑）与未配置在**运行时效果一致**（都没有 server），统一落 NULL，
    让 DB 只保留「无 vs 精确子集」两种物理状态。
    """
    if not value:  # None / [] / '' 全归 NULL
        return None
    return _dumps(value)


def _to_row(data: dict[str, Any]) -> dict[str, Any]:
    """把写入方传入的业务 dict 规整为列值 dict（含时间戳），交给 INSERT/UPDATE。"""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "name": data["name"],
        "origin": data.get("origin", "builtin"),
        "role": data["role"],
        "stage": data.get("stage", "other"),
        "description": _opt_str(data.get("description")),
        "system_prompt": _opt_str(data.get("system_prompt")),
        "schema_json": _dumps(data.get("schema")),
        "mcp_server_ids": _opt_ids(data.get("mcp_server_ids")),
        "enabled": 1 if data.get("enabled", True) else 0,
        "reasoning_enabled": 1 if data.get("reasoning_enabled", False) else 0,
        "updated_at": now,
    }


def _from_row(r: aiosqlite.Row) -> dict[str, Any]:
    """把 DB 行解析为对外业务 dict（schema / mcp_server_ids 反序列化为 Python 对象）。"""
    return {
        "name": r["name"],
        "origin": r["origin"],
        "role": r["role"],
        "stage": r["stage"],
        "description": r["description"],
        "system_prompt": r["system_prompt"],
        "schema": json.loads(r["schema_json"]) if r["schema_json"] is not None else None,
        "mcp_server_ids": json.loads(r["mcp_server_ids"]) if r["mcp_server_ids"] is not None else None,
        "enabled": bool(r["enabled"]),
        "reasoning_enabled": bool(r["reasoning_enabled"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


class AgentConfigStore:
    """AgentSpec 配置的轻量 CRUD 存储（aiosqlite，惰性连接）。"""

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
        """预留结构迁移钩子（当前新表无历史，直接返回）。"""
        cur = await self._c.execute("PRAGMA table_info(agent_configs)")
        existing = {row["name"] for row in await cur.fetchall()}
        for col, decl in _MIGRATION_COLUMNS.items():
            if col not in existing:
                await self._c.execute(f"ALTER TABLE agent_configs ADD COLUMN {col} {decl}")
        for col in _DROP_COLUMNS:
            if col in existing:
                await self._c.execute(f"ALTER TABLE agent_configs DROP COLUMN {col}")

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "AgentConfigStore 未 connect()"
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def save(self, data: dict[str, Any]) -> str:
        """保存一条 agent 配置，返回 name。name 冲突抛 sqlite3.IntegrityError。"""
        await self.connect()
        col = _to_row(data)
        await self._c.execute(
            "INSERT INTO agent_configs"
            "(name, origin, role, stage, description, system_prompt, schema_json,"
            " mcp_server_ids, enabled, reasoning_enabled, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (col["name"], col["origin"], col["role"], col["stage"], col["description"],
             col["system_prompt"], col["schema_json"], col["mcp_server_ids"], col["enabled"],
             col["reasoning_enabled"], col["updated_at"], col["updated_at"]),
        )
        await self._c.commit()
        return col["name"]

    async def list(self) -> list[dict[str, Any]]:
        """全部记录（按创建时间倒序）。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT * FROM agent_configs ORDER BY created_at DESC"
        )
        return [_from_row(r) for r in await cur.fetchall()]

    async def get(self, name: str) -> dict[str, Any] | None:
        """读取完整记录。"""
        await self.connect()
        cur = await self._c.execute(
            "SELECT * FROM agent_configs WHERE name=?", (name,)
        )
        row = await cur.fetchone()
        return _from_row(row) if row is not None else None

    async def update(self, name: str, data: dict[str, Any]) -> bool:
        """更新除 name/origin/created_at 外的列（origin 结构性不可改），返回是否命中。"""
        await self.connect()
        col = _to_row(data)
        cur = await self._c.execute(
            "UPDATE agent_configs SET role=?, stage=?, description=?, system_prompt=?,"
            " schema_json=?, mcp_server_ids=?, enabled=?, reasoning_enabled=?,"
            " updated_at=? WHERE name=?",
            (col["role"], col["stage"], col["description"], col["system_prompt"],
             col["schema_json"], col["mcp_server_ids"], col["enabled"],
             col["reasoning_enabled"], col["updated_at"], name),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def delete(self, name: str) -> bool:
        """删除，返回是否命中。"""
        await self.connect()
        cur = await self._c.execute("DELETE FROM agent_configs WHERE name=?", (name,))
        await self._c.commit()
        return cur.rowcount == 1


# ----------------------------------------------------------------------
# PostgreSQL 后端（state_store=postgres 时由 build_agent_config_store 选择）
# ----------------------------------------------------------------------
# 列 schema 与 sqlite AgentConfigStore 完全一致（JSON 存 TEXT、bool 存 INTEGER、时间戳 ISO TEXT），
# sqlite 的 _to_row/_from_row 直接复用：插入参数序 / 读出 dict 形状两端相同。
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_configs (
    name            TEXT PRIMARY KEY,
    origin          TEXT NOT NULL DEFAULT 'builtin',
    role            TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'other',
    description     TEXT,
    system_prompt   TEXT,
    schema_json     TEXT,
    mcp_server_ids  TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    reasoning_enabled INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class PgAgentConfigStore:
    """AgentConfigStore 的 PostgreSQL 实现（psycopg3 async，单连接，幂等 connect）。

    方法面 / 列结构 / 返回形状与 sqlite ``AgentConfigStore`` 一致，供 API 层无感切换。
    name 唯一冲突：PG 的 UniqueViolation 收敛为 ``sqlite3.IntegrityError`` 抛出——
    app 端点本就捕获它映射为 400 中文「name 已存在」，避免跨库漏处理（同 mcp_store 做法）。
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
        # 老库结构迁移：表早已存在时 IF NOT EXISTS 不补列 → 按 _MIGRATION_COLUMNS 受保护 ALTER
        # （information_schema 查列，缺才 ADD；与 sqlite _migrate() PRAGMA 语义一致）。
        cur = await self._conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='agent_configs'"
        )
        existing = {row["column_name"] for row in await cur.fetchall()}
        for col, decl in _MIGRATION_COLUMNS.items():
            if col not in existing:
                await self._conn.execute(f"ALTER TABLE agent_configs ADD COLUMN {col} {decl}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self):
        assert self._conn is not None, "PgAgentConfigStore 未 connect()"
        return self._conn

    async def save(self, data: dict[str, Any]) -> str:
        """保存一条配置，返回 name。name 重复 → sqlite3.IntegrityError（语义同 sqlite）。"""
        await self.connect()
        col = _to_row(data)
        try:
            await self._c.execute(
                "INSERT INTO agent_configs"
                "(name, origin, role, stage, description, system_prompt, schema_json,"
                " mcp_server_ids, enabled, reasoning_enabled, created_at, updated_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (col["name"], col["origin"], col["role"], col["stage"], col["description"],
                 col["system_prompt"], col["schema_json"], col["mcp_server_ids"], col["enabled"],
                 col["reasoning_enabled"], col["updated_at"], col["updated_at"]),
            )
        except Exception as exc:  # noqa: BLE001 —— 只认 unique violation，其余原样上抛
            from psycopg.errors import UniqueViolation

            if isinstance(exc, UniqueViolation):
                raise sqlite3.IntegrityError("UNIQUE constraint failed: agent_configs.name") from exc
            raise
        await self._c.commit()
        return col["name"]

    async def list(self) -> list[dict[str, Any]]:
        await self.connect()
        cur = await self._c.execute("SELECT * FROM agent_configs ORDER BY created_at DESC")
        return [_from_row(r) for r in await cur.fetchall()]

    async def get(self, name: str) -> dict[str, Any] | None:
        await self.connect()
        cur = await self._c.execute("SELECT * FROM agent_configs WHERE name=%s", (name,))
        row = await cur.fetchone()
        return _from_row(row) if row is not None else None

    async def update(self, name: str, data: dict[str, Any]) -> bool:
        await self.connect()
        col = _to_row(data)
        cur = await self._c.execute(
            "UPDATE agent_configs SET role=%s, stage=%s, description=%s, system_prompt=%s,"
            " schema_json=%s, mcp_server_ids=%s, enabled=%s, reasoning_enabled=%s,"
            " updated_at=%s WHERE name=%s",
            (col["role"], col["stage"], col["description"], col["system_prompt"],
             col["schema_json"], col["mcp_server_ids"], col["enabled"],
             col["reasoning_enabled"], col["updated_at"], name),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def delete(self, name: str) -> bool:
        await self.connect()
        cur = await self._c.execute("DELETE FROM agent_configs WHERE name=%s", (name,))
        await self._c.commit()
        return cur.rowcount == 1


def build_agent_config_store(settings) -> AgentConfigStore:
    """按 state_store 选择控制面 AgentSpec 配置存储后端（对齐 build_mcp_store）。

    - sqlite/memory（本地 MVP）：沿用 settings.state_db_path 的 aiosqlite 实现；
    - postgres（生产 M6）：agent 配置也落 PG，与运行期/其它控制面表同库。
    """
    if settings.state_store == "postgres":
        return PgAgentConfigStore(postgres_dsn(settings))
    return AgentConfigStore(settings.state_db_path)


async def seed_builtin_agent_configs(store) -> int:
    """seed / 补齐 15 条内置 agent 配置（幂等），返回本次写入+回填的行数。

    - 表空：完整插入 15 条 builtin。除 origin/role/stage/description 外，**system_prompt /
      schema_json 也一并落静态默认**（不再留 NULL 空列，DB 即该内置 agent 的可见配置快照）；
      mcp_server_ids 仍 NULL（=无 MCP server，两态语义）、enabled=1。
    - 表非空：只对**已存在的内置行**做缺失默认回填——若某内置行的 system_prompt/schema_json
      仍为 NULL（历史 seed 空列 / 用户清空过），补齐为当前静态默认；不动 role/stage/description/
      enabled/mcp_server_ids 与用户已填的非空覆盖值。不补插缺失内置（避免 clobber 仅留自定义态）。
    - 物化后语义：内置行是非 NULL 快照 → 改代码 SYSTEM_PROMPTS/AGENT_SCHEMAS 不再自动生效；
      要让某内置跟随代码，UI 清空该字段保存（→NULL 即回退），下次启动回填会固化为当前默认。
    """
    from ..agents.registry import AGENT_DESCRIPTIONS, AGENT_STAGES, DIAGNOSE_AGENTS, FIX_AGENTS
    from ..agents.prompts import AGENT_SCHEMAS, SYSTEM_PROMPTS

    def _defaults_for(name: str) -> dict[str, Any]:
        return {
            "origin": "builtin",
            "role": "diagnose" if name in DIAGNOSE_AGENTS else "fix",
            "stage": AGENT_STAGES.get(name, "other"),
            "description": AGENT_DESCRIPTIONS.get(name, ""),
            "system_prompt": SYSTEM_PROMPTS.get(name),
            "schema": AGENT_SCHEMAS.get(name),
            "mcp_server_ids": None,  # NULL → 无 MCP server（需在 UI 显式绑定才有）
            "enabled": True,
            "reasoning_enabled": False,  # 默认常规模型；按 agent 需 CoT 时 UI/API 翻 true
        }

    names = DIAGNOSE_AGENTS + FIX_AGENTS
    rows = await store.list()
    if not rows:
        written = 0
        for name in names:
            await store.save({"name": name, **_defaults_for(name)})
            written += 1
        return written
    # 非空：仅回填既有内置行的缺失默认（system_prompt / schema_json 为 NULL 者）
    by_name = {r["name"]: r for r in rows}
    written = 0
    for name in names:
        existing = by_name.get(name)
        if existing is None:
            continue
        patch = dict(existing)
        patch["name"] = name
        changed = False
        if existing["system_prompt"] is None and SYSTEM_PROMPTS.get(name):
            patch["system_prompt"] = SYSTEM_PROMPTS[name]
            changed = True
        if existing["schema"] is None and AGENT_SCHEMAS.get(name):
            patch["schema"] = AGENT_SCHEMAS[name]
            changed = True
        if changed:
            await store.update(name, patch)
            written += 1
    return written
