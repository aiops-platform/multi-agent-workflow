# -*- coding: utf-8 -*-
"""AgentConfigStore 测试：sqlite CRUD / name 冲突 / ""→NULL 归整 / JSON 列往返 /
seed 幂等 / builder 选择。

用内存库（AgentConfigStore(":memory:")）跑存取语义，不碰磁盘文件；builder 选择用
Settings(...) 构造（_env_file=None 屏蔽仓库 .env），PG 端仅断言类型与 dsn 前缀
（psycopg 惰性 import，不连真实 DB）。
"""
import sqlite3

import pytest

from agentflow.agents.prompts import AGENT_SCHEMAS, SYSTEM_PROMPTS
from agentflow.agents.registry import AGENT_DESCRIPTIONS, AGENT_STAGES, DIAGNOSE_AGENTS, FIX_AGENTS
from agentflow.api.agent_store import (
    AgentConfigStore,
    PgAgentConfigStore,
    build_agent_config_store,
    seed_builtin_agent_configs,
)
from agentflow.config import Settings

BUILTIN_15 = DIAGNOSE_AGENTS + FIX_AGENTS


def _row(**over) -> dict:
    row = {
        "name": "custom-diag",
        "origin": "custom",
        "role": "diagnose",
        "stage": "diagnose",
        "description": "自定义诊断体",
        "system_prompt": "你是诊断体。",
        "schema": {"type": "object"},
        "mcp_server_ids": ["m1"],
        "enabled": True,
    }
    row.update(over)
    return row


async def test_save_get_list_roundtrip() -> None:
    store = AgentConfigStore(":memory:")
    name = await store.save(_row())
    got = await store.get(name)
    assert got["name"] == "custom-diag"
    assert got["origin"] == "custom"
    assert got["role"] == "diagnose"
    assert got["description"] == "自定义诊断体"
    assert got["system_prompt"] == "你是诊断体。"
    assert got["schema"] == {"type": "object"}
    assert got["mcp_server_ids"] == ["m1"]
    assert got["enabled"] is True
    assert got["created_at"] and got["updated_at"]
    assert [r["name"] for r in await store.list()] == ["custom-diag"]
    await store.close()


async def test_reasoning_enabled_column_roundtrip_and_default() -> None:
    """reasoning_enabled 列：默认 0（False）；True 存取往返。"""
    store = AgentConfigStore(":memory:")
    await store.save(_row())
    assert (await store.get("custom-diag"))["reasoning_enabled"] is False  # 缺省 → False
    await store.save(_row(name="rz", reasoning_enabled=True))
    assert (await store.get("rz"))["reasoning_enabled"] is True
    await store.update("rz", _row(name="rz", reasoning_enabled=False))
    assert (await store.get("rz"))["reasoning_enabled"] is False
    await store.close()


async def test_sqlite_legacy_table_gains_reasoning_enabled(tmp_path) -> None:
    """老库（reasoning_enabled 列加入前建的表）→ connect 受保护 ALTER 补列 → 读写正常。"""
    import aiosqlite

    db = tmp_path / "legacy.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "CREATE TABLE agent_configs ("
            " name TEXT PRIMARY KEY, origin TEXT NOT NULL DEFAULT 'builtin',"
            " role TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'other',"
            " description TEXT, system_prompt TEXT, schema_json TEXT,"
            " mcp_server_ids TEXT, enabled INTEGER NOT NULL DEFAULT 1,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        await conn.commit()
    store = AgentConfigStore(db)
    await store.save(_row(name="old", system_prompt="sp"))
    assert (await store.get("old"))["reasoning_enabled"] is False  # 补列默认 0
    await store.update("old", _row(name="old", reasoning_enabled=True))
    assert (await store.get("old"))["reasoning_enabled"] is True
    await store.close()


async def test_save_duplicate_name_raises_integrity_error() -> None:
    store = AgentConfigStore(":memory:")
    await store.save(_row(name="dup"))
    with pytest.raises(sqlite3.IntegrityError):
        await store.save(_row(name="dup"))
    await store.close()


async def test_empty_string_and_whitespace_normalized_to_null() -> None:
    """''/空白 → NULL（未覆盖 → 运行时回退内置静态默认）。"""
    store = AgentConfigStore(":memory:")
    await store.save(_row(description="", system_prompt="   ", schema="", mcp_server_ids=""))
    got = await store.get("custom-diag")
    assert got["description"] is None
    assert got["system_prompt"] is None
    assert got["schema"] is None
    assert got["mcp_server_ids"] is None
    await store.close()


async def test_empty_list_normalized_to_null() -> None:
    """两态（v1.12.1）：[]（明确不绑）与 NULL（未配置）运行时同为「无 server」→ 写入归一 NULL。"""
    store = AgentConfigStore(":memory:")
    await store.save(_row(name="bound-none", mcp_server_ids=[]))
    await store.save(_row(name="bound-null", mcp_server_ids=None))
    assert (await store.get("bound-none"))["mcp_server_ids"] is None  # [] 归一 NULL
    assert (await store.get("bound-null"))["mcp_server_ids"] is None
    # 非空数组仍保真为精确子集
    await store.save(_row(name="bound-sub", mcp_server_ids=["m1", "m2"]))
    assert (await store.get("bound-sub"))["mcp_server_ids"] == ["m1", "m2"]
    await store.close()


async def test_update_overrides_columns_only() -> None:
    """update 不改 name/origin/created_at；未命中返回 False。"""
    store = AgentConfigStore(":memory:")
    await store.save(_row())
    origin_before = (await store.get("custom-diag"))["origin"]
    ok = await store.update(
        "custom-diag",
        _row(description="新描述", system_prompt="新提示", mcp_server_ids=["m2"], enabled=False),
    )
    assert ok is True
    got = await store.get("custom-diag")
    assert got["description"] == "新描述"
    assert got["system_prompt"] == "新提示"
    assert got["mcp_server_ids"] == ["m2"]
    assert got["enabled"] is False
    assert got["origin"] == origin_before  # origin 结构性不可改
    assert (await store.update("nope", _row(name="nope"))) is False
    await store.close()


async def test_delete_returns_hit() -> None:
    store = AgentConfigStore(":memory:")
    await store.save(_row())
    assert await store.delete("custom-diag") is True
    assert await store.get("custom-diag") is None
    assert await store.delete("custom-diag") is False
    await store.close()


async def test_seed_empty_table_writes_15_builtins() -> None:
    store = AgentConfigStore(":memory:")
    assert await seed_builtin_agent_configs(store) == 15
    rows = await store.list()
    assert {r["name"] for r in rows} == set(BUILTIN_15)
    by_name = {r["name"]: r for r in rows}
    for r in rows:
        assert r["origin"] == "builtin"
        assert r["enabled"] is True
        assert r["reasoning_enabled"] is False  # 默认常规模型（需 CoT 的 agent 显式开启）
        assert r["mcp_server_ids"] is None  # 未配置 → 无 MCP server（两态，UI 绑定才有）
        assert r["system_prompt"] == SYSTEM_PROMPTS[r["name"]]  # 内置默认已物化
        assert r["schema"] == AGENT_SCHEMAS[r["name"]]
        assert r["description"] == AGENT_DESCRIPTIONS[r["name"]]
        assert r["stage"] == AGENT_STAGES[r["name"]]
    assert by_name["triage"]["role"] == "diagnose"
    assert by_name["triage"]["stage"] == "detect"
    await store.close()


async def test_seed_non_empty_only_custom_no_builtin_skips() -> None:
    """表非空且无内置行 → 不动（不 clobber、也不补插缺失内置）。"""
    store = AgentConfigStore(":memory:")
    await store.save(_row())
    assert await seed_builtin_agent_configs(store) == 0
    assert [r["name"] for r in await store.list()] == ["custom-diag"]
    await store.close()


async def test_seed_empty_then_idempotent() -> None:
    """先 seed（空表 15 条，默认全物化）再 seed → 第二次 0。"""
    store = AgentConfigStore(":memory:")
    assert await seed_builtin_agent_configs(store) == 15
    assert await seed_builtin_agent_configs(store) == 0
    await store.close()


async def test_seed_backfills_null_prompt_schema_on_existing_builtins() -> None:
    """既有内置行（历史 seed 空列）→ 回填静态默认；用户编辑的非空字段/自定义行不动。"""
    d0, f0 = DIAGNOSE_AGENTS[0], FIX_AGENTS[0]
    store = AgentConfigStore(":memory:")
    # d0：旧 seed 空列 + 用户改过 description/enabled/mcp 绑定 → 只补 prompt/schema
    await store.save({
        "name": d0, "origin": "builtin", "role": "diagnose",
        "stage": AGENT_STAGES[d0], "description": "用户改过描述",
        "system_prompt": None, "schema": None, "mcp_server_ids": ["m1"], "enabled": False,
    })
    # f0：prompt 已覆盖（非 NULL）→ 只补 schema
    await store.save({
        "name": f0, "origin": "builtin", "role": "fix",
        "stage": AGENT_STAGES[f0], "description": AGENT_DESCRIPTIONS[f0],
        "system_prompt": "覆盖提示", "schema": None, "mcp_server_ids": None, "enabled": True,
    })
    # custom 行不参与回填
    await store.save(_row())
    assert await seed_builtin_agent_configs(store) == 2
    got_d0 = await store.get(d0)
    assert got_d0["system_prompt"] == SYSTEM_PROMPTS[d0]
    assert got_d0["schema"] == AGENT_SCHEMAS[d0]
    assert got_d0["description"] == "用户改过描述"  # 用户编辑不被覆盖
    assert got_d0["enabled"] is False
    assert got_d0["mcp_server_ids"] == ["m1"]
    got_f0 = await store.get(f0)
    assert got_f0["system_prompt"] == "覆盖提示"  # 非空覆盖不被覆盖
    assert got_f0["schema"] == AGENT_SCHEMAS[f0]
    assert (await store.get("custom-diag"))["system_prompt"] == "你是诊断体。"
    # 二次 seed → 无 NULL 可补 → 0（幂等）
    assert await seed_builtin_agent_configs(store) == 0
    await store.close()


def test_builder_selects_sqlite_default() -> None:
    s = Settings(state_store="sqlite", state_db_path=":memory:", _env_file=None)
    store = build_agent_config_store(s)
    assert isinstance(store, AgentConfigStore)
    assert not isinstance(store, PgAgentConfigStore)


def test_builder_selects_pg_when_state_store_postgres() -> None:
    s = Settings(state_store="postgres", postgres_dsn="localhost:5432/cfg?user=u&password=p", _env_file=None)
    store = build_agent_config_store(s)
    assert isinstance(store, PgAgentConfigStore)
    assert store._dsn == "postgresql://localhost:5432/cfg?user=u&password=p"
