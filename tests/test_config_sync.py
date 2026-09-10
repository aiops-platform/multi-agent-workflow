"""Worker 配置热载（`agents/config_sync.py` + `MCPClientManager.revalidate`）。

回归背景：Worker 的 agent 配置解析器曾是**永久缓存**——绑定新 MCP server 后必须
重启 Worker 才生效（Worker 与 API 是不同进程，看不到 API 侧的内存代际计数器）。
"""
from __future__ import annotations

import pytest

from agentflow.agents.config_sync import TenantConfigSync, _signature
from agentflow.agents.mcp_manager import MCPClientManager


# ----------------------------------------------------------------------
# 假 store / router：只需覆盖 config_sync 用到的那几个方法
# ----------------------------------------------------------------------
class _FakeAgentStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def list(self) -> list[dict]:
        return list(self.rows)


class _FakeMcpStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def list(self) -> list[dict]:
        return list(self.rows)

    async def list_enabled(self) -> list[dict]:
        return [r for r in self.rows if r.get("enabled")]

    async def get(self, mid: str) -> dict | None:
        return next((r for r in self.rows if r["id"] == mid), None)


class _FakeBundle:
    def __init__(self) -> None:
        self.agent_config = _FakeAgentStore()
        self.mcp = _FakeMcpStore()


class _FakeRouter:
    def __init__(self, bundle: _FakeBundle) -> None:
        self.bundle = bundle
        self.get_calls = 0

    async def get(self, _key: str) -> _FakeBundle:
        self.get_calls += 1
        return self.bundle


def _agent_row(name: str, updated: str, server_ids=None) -> dict:
    return {"name": name, "origin": "builtin", "role": "diagnose", "stage": "detect",
            "enabled": True, "reasoning_enabled": None,
            "mcp_server_ids": server_ids, "updated_at": updated}


def _mcp_row(mid: str, updated: str, *, enabled: bool = True) -> dict:
    return {"id": mid, "name": f"srv-{mid}", "transport": "http",
            "config": {"url": "http://127.0.0.1:9999/mcp"}, "is_stateful": False,
            "enabled": enabled, "updated_at": updated}


@pytest.fixture
def wired():
    bundle = _FakeBundle()
    router = _FakeRouter(bundle)
    sync = TenantConfigSync(router, mcp_manager=None, interval=0.0)  # 0 = 每次都查
    return bundle, router, sync


# ----------------------------------------------------------------------
# 指纹
# ----------------------------------------------------------------------
def test_signature_covers_count_and_timestamp() -> None:
    assert _signature([]) == (0, "")
    assert _signature([{"updated_at": "t1"}, {"updated_at": "t2"}]) == (2, "t2")
    # 删掉非最新一条 → 行数变（max 不变也要能识别）
    assert _signature([{"updated_at": "t1"}, {"updated_at": "t2"}]) != _signature(
        [{"updated_at": "t2"}]
    )


# ----------------------------------------------------------------------
# 解析器热载
# ----------------------------------------------------------------------
async def test_resolver_reflects_new_binding_without_restart(wired) -> None:
    """绑定变更后**无需重启**：下次取解析器即生效。"""
    bundle, _router, sync = wired
    bundle.agent_config.rows = [_agent_row("metrics-analyst", "t1")]

    r1 = await sync.resolver("local")
    assert r1.server_ids_for("metrics-analyst") == set()  # 尚未绑定

    # 模拟 API 侧 PUT /agent-configs 写入绑定（updated_at 随之变化）
    bundle.agent_config.rows = [_agent_row("metrics-analyst", "t2", server_ids=["mid-1"])]

    r2 = await sync.resolver("local")
    assert r2.server_ids_for("metrics-analyst") == {"mid-1"}
    assert r2 is not r1  # 确实重建了，而非复用旧实例


async def test_resolver_reuses_when_unchanged(wired) -> None:
    """配置没变 → 复用同一实例（不做无谓重建）。"""
    bundle, _router, sync = wired
    bundle.agent_config.rows = [_agent_row("triage", "t1")]

    r1 = await sync.resolver("local")
    r2 = await sync.resolver("local")
    assert r1 is r2


async def test_interval_gates_db_queries(wired) -> None:
    """间隔门控：窗口内不重复查库；窗口外才查。"""
    bundle, router, _ = wired
    bundle.agent_config.rows = [_agent_row("triage", "t1")]
    sync = TenantConfigSync(router, mcp_manager=None, interval=3600.0)

    await sync.resolver("local")
    assert router.get_calls == 1
    await sync.resolver("local")
    await sync.resolver("local")
    assert router.get_calls == 1  # 窗口内：不再查库


async def test_mcp_server_change_also_triggers_rebuild(wired) -> None:
    """MCP server 本身变化（非 agent 绑定）也要触发重建。"""
    bundle, _router, sync = wired
    bundle.agent_config.rows = [_agent_row("triage", "t1")]
    bundle.mcp.rows = [_mcp_row("mid-1", "t1")]

    r1 = await sync.resolver("local")
    bundle.mcp.rows = [_mcp_row("mid-1", "t2")]  # server 配置改了
    r2 = await sync.resolver("local")
    assert r2 is not r1


# ----------------------------------------------------------------------
# MCPClientManager.revalidate
# ----------------------------------------------------------------------
async def test_revalidate_evicts_deleted_server() -> None:
    """库里删除的 server 必须被淘汰——只清 _loaded 不够，_load_row 会早退。"""
    bundle = _FakeBundle()
    mgr = MCPClientManager(bundle.mcp, stores_provider=lambda _t: _aw(bundle.mcp))
    bundle.mcp.rows = [_mcp_row("mid-1", "t1")]
    await mgr.load("local")
    assert ("local", "mid-1") in mgr._clients

    bundle.mcp.rows = []  # 删除
    changed = await mgr.revalidate("local")
    assert changed is True
    assert ("local", "mid-1") not in mgr._clients


async def test_revalidate_evicts_disabled_server() -> None:
    bundle = _FakeBundle()
    mgr = MCPClientManager(bundle.mcp, stores_provider=lambda _t: _aw(bundle.mcp))
    bundle.mcp.rows = [_mcp_row("mid-1", "t1")]
    await mgr.load("local")

    bundle.mcp.rows = [_mcp_row("mid-1", "t1", enabled=False)]  # 禁用
    assert await mgr.revalidate("local") is True
    assert ("local", "mid-1") not in mgr._clients


async def test_revalidate_evicts_changed_config() -> None:
    bundle = _FakeBundle()
    mgr = MCPClientManager(bundle.mcp, stores_provider=lambda _t: _aw(bundle.mcp))
    bundle.mcp.rows = [_mcp_row("mid-1", "t1")]
    await mgr.load("local")
    old = mgr._clients[("local", "mid-1")]

    bundle.mcp.rows = [_mcp_row("mid-1", "t2")]  # 配置变（updated_at 变）
    assert await mgr.revalidate("local") is True
    assert mgr._clients.get(("local", "mid-1")) is not old


async def test_revalidate_keeps_unchanged_and_allows_reload() -> None:
    """未变化的保留（不重建）；_loaded 被清以便补充新增项。"""
    bundle = _FakeBundle()
    mgr = MCPClientManager(bundle.mcp, stores_provider=lambda _t: _aw(bundle.mcp))
    bundle.mcp.rows = [_mcp_row("mid-1", "t1")]
    await mgr.load("local")
    kept = mgr._clients[("local", "mid-1")]

    assert await mgr.revalidate("local") is False  # 无变化
    assert mgr._clients[("local", "mid-1")] is kept  # 原 client 保留
    assert "local" not in mgr._loaded  # 但仍允许下次重扫（补新增）


async def test_revalidate_then_load_picks_up_new_server() -> None:
    """新增 server：revalidate 清 _loaded 后，下次访问即可见。"""
    bundle = _FakeBundle()
    mgr = MCPClientManager(bundle.mcp, stores_provider=lambda _t: _aw(bundle.mcp))
    bundle.mcp.rows = [_mcp_row("mid-1", "t1")]
    await mgr.load("local")

    bundle.mcp.rows.append(_mcp_row("mid-2", "t2"))  # 新注册
    await mgr.revalidate("local")
    await mgr._ensure_loaded("local")  # 模拟 clients_for_agent 的惰性加载
    assert ("local", "mid-2") in mgr._clients


async def _aw(value):
    return value
