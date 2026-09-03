# -*- coding: utf-8 -*-
"""MCPStore CRUD 存储测试：/mcp_servers 表往返 / 列表 / 更新·删除哨兵 / enabled 过滤。

对齐 test_workflow_store 语义；MCPStore 是 aiosqlite 惰性连接，测试可直接用（无需 lifespan）。
"""
import sqlite3

import pytest

from agentflow.api.mcp_store import MCPStore


def _row(name: str = "mock-mcp", *, transport: str = "stdio", **over) -> dict:
    if transport == "stdio":
        config = {"command": "python", "args": ["-m", "mock"], "env": None, "cwd": None}
    else:
        config = {"url": "http://127.0.0.1:9999/mcp"}
    row = {
        "name": name,
        "transport": transport,
        "config": config,
        "is_stateful": transport == "stdio",
        "enable_tools": None,
        "disable_tools": None,
        "enabled": True,
    }
    row.update(over)
    return row


@pytest.fixture
async def store(tmp_path):
    s = MCPStore(tmp_path / "mcp.db")
    yield s
    await s.close()


async def test_save_get_roundtrip(store) -> None:
    mid = await store.save(_row())
    got = await store.get(mid)
    assert got is not None
    assert got["id"] == mid
    assert got["name"] == "mock-mcp"
    assert got["transport"] == "stdio"
    assert got["is_stateful"] is True  # bool 往返（落库为 INTEGER）
    assert got["config"]["command"] == "python"
    assert "agents" not in got  # server 侧绑定字段已移除
    assert got["enable_tools"] is None
    assert got["created_at"] and got["updated_at"]


async def test_save_http_row_is_stateful_false(store) -> None:
    mid = await store.save(_row("remote", transport="http", is_stateful=False))
    got = await store.get(mid)
    assert got["is_stateful"] is False
    assert got["config"]["url"] == "http://127.0.0.1:9999/mcp"


async def test_save_duplicate_name_raises(store) -> None:
    await store.save(_row("dup"))
    with pytest.raises(sqlite3.IntegrityError):
        await store.save(_row("dup"))


async def test_list_returns_all_and_sortable(store) -> None:
    a = await store.save(_row("aaa"))
    b = await store.save(_row("bbb", transport="http", is_stateful=False))
    rows = await store.list()
    assert {r["id"] for r in rows} == {a, b}
    assert {r["name"] for r in rows} == {"aaa", "bbb"}


async def test_list_enabled_filters(store) -> None:
    await store.save(_row("enabled-1"))
    disabled = await store.save(_row("disabled", enabled=False))
    assert {r["name"] for r in await store.list_enabled()} == {"enabled-1"}
    # get 仍能拿到 disabled 行（CRUD 全量视图）
    assert (await store.get(disabled))["enabled"] is False


async def test_update_returns_hit_and_persists(store) -> None:
    mid = await store.save(_row("orig"))
    ok = await store.update(mid, _row("renamed", transport="http", is_stateful=False))
    assert ok is True
    got = await store.get(mid)
    assert got["name"] == "renamed"
    assert got["transport"] == "http"
    assert got["is_stateful"] is False
    assert "agents" not in got
    # miss 哨兵
    assert await store.update("nope", _row("x")) is False


async def test_delete_returns_hit(store) -> None:
    mid = await store.save(_row("gone"))
    assert await store.delete(mid) is True
    assert await store.get(mid) is None
    assert await store.delete(mid) is False


async def test_save_get_tools_snapshot(store) -> None:
    tools = [{"name": "get_weather", "description": "x", "read_only": True, "llm_name": "mcp__m__get_weather"}]
    mid = await store.save(_row("snap", tools=tools))
    got = await store.get(mid)
    assert got["tools"] == tools
    # 缺省 tools → 存 null
    mid2 = await store.save(_row("nosnap"))
    assert (await store.get(mid2))["tools"] is None


async def test_update_tools_writes_back(store) -> None:
    mid = await store.save(_row("snap"))
    tools = [{"name": "query.repo", "read_only": True, "llm_name": "mcp__m__queryxrepo"}]
    assert await store.update_tools(mid, tools) is True
    assert (await store.get(mid))["tools"] == tools
    assert await store.update_tools("nope", tools) is False  # miss 哨兵
    assert await store.update_tools(mid, None) is True  # 显式清空
    assert (await store.get(mid))["tools"] is None


async def test_legacy_db_migrates_adds_tools_drops_agents(tmp_path) -> None:
    """旧库 connect 自动迁移：补 tools 列 + 丢弃已废弃的 agents 列（server 侧绑定）。

    旧行保留（tools=None），update_tools 可用；agent 绑定改到 agent 主表侧建模。
    """
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE mcp_servers ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, transport TEXT NOT NULL,"
        " config TEXT NOT NULL, is_stateful INTEGER NOT NULL DEFAULT 1, agents TEXT NOT NULL,"
        " enable_tools TEXT, disable_tools TEXT, enabled INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        " INSERT INTO mcp_servers (id,name,transport,config,is_stateful,agents,enabled,created_at,updated_at)"
        " VALUES ('l1','legacy','http','{\"url\":\"http://x/mcp\"}',0,'[\"triage\"]',1,'c','u');"
    )
    con.commit()
    con.close()
    s = MCPStore(db)
    try:
        await s.connect()  # 触发迁移
        got = await s.get("l1")
        assert got is not None and got["name"] == "legacy"
        assert got["tools"] is None
        assert "agents" not in got
        assert await s.update_tools("l1", [{"name": "x"}]) is True
        assert (await s.get("l1"))["tools"] == [{"name": "x"}]
    finally:
        await s.close()
    # 物理列：tools 已补、agents 已删
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(mcp_servers)")]
    con.close()
    assert "tools" in cols and "agents" not in cols
