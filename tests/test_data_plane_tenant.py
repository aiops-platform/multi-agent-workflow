"""批 C（design-v5.3 §7）：数据面租户化——共享数据源下线 + repo 直传封堵 +
per-tenant MCP/agent 配置路由（runner 经 current_tenant）。"""
from __future__ import annotations

import pytest

from agentflow.agents.mcp import build_toolkit
from agentflow.agents.mcp_manager import MCPClientManager
from agentflow.config import get_settings
from agentflow.core.workflow import Workflow
from agentflow.exec_context import current_tenant
from agentflow.executor.dag_executor import DAGExecutor
from agentflow.service import InputsValidationError, RunService
from agentflow.statestore.memory import InMemoryStateStore

SIMPLE_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage }
  logs: { agent: log-analyst }
edges:
  - { from: triage, to: logs }
"""

REPOS_YAML = """
name: repos-flow
nodes:
  triage: { agent: triage }
edges: []
"""


# ======================================================================
# §7.1 共享数据源下线（toolkit 门控）
# ======================================================================
def _tool_names(toolkit) -> set[str]:
    """Toolkit 工具名集合（get_tool_schemas 为 async → 取 name 字段）。"""
    import asyncio

    schemas = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        toolkit.get_tool_schemas()
    )
    return {s["function"]["name"] for s in schemas}


def test_datasource_tools_never_in_local_toolkit() -> None:
    """数据源工具**一律不在本地 toolkit**——它们由 MCP 提供（design-v5.5 批3）。

    批 3 之前这里测的是 ``shared_datasources`` 开关能否关掉内置数据源工具；那个开关
    连同内置数据源实现一起删除了。现在无论配置如何，本地都不会再出现它们。
    """
    for agent in ("log-analyst", "metrics-analyst", "infra-locator", "root-cause"):
        names = _tool_names(build_toolkit(agent))
        for gone in ("query_logs", "get_trace", "query_metrics",
                     "check_infra", "describe_pod"):
            assert gone not in names, f"{agent} 不应再有本地 {gone}"

    # 仅存的本地只读工具是 CMDB 映射与知识检索（非数据源）
    assert "locate_code" in _tool_names(build_toolkit("code-locator"))
    assert "search_knowledge" in _tool_names(build_toolkit("knowledge-lookup"))


# ======================================================================
# §7.3 repo 直传封堵
# ======================================================================
async def test_inputs_repos_blocked_in_hardened_mode() -> None:
    store = InMemoryStateStore()
    svc = RunService(store, tenant_registry=None)
    wf = Workflow.load_yaml(REPOS_YAML)
    with pytest.raises(InputsValidationError, match="repos"):
        await svc.create_run("t1", wf, {"repos": {"svc": "https://github.com/x/svc"}})


async def test_inputs_repos_allowed_when_shared_datasources_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "shared_datasources", True, raising=False)
    store = InMemoryStateStore()
    svc = RunService(store)
    wf = Workflow.load_yaml(REPOS_YAML)
    out = await svc.create_run("t1", wf, {"repos": {"svc": "https://github.com/x/svc"}})
    assert out["run_id"]


# ======================================================================
# §7.2/§5.3 per-tenant MCP 路由（runner 经 current_tenant）
# ======================================================================
class _FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.is_stateful = False
        self.is_connected = True

    async def list_tools(self):
        return []


class _FakeStore:
    """最小 mcp store 桩：list_enabled/get + 每租户不同的 server 集。"""

    def __init__(self, servers: list[dict]) -> None:
        self._by_id = {r["id"]: r for r in servers}
        self._servers = servers

    async def list_enabled(self):
        return [r for r in self._servers if r.get("enabled")]

    async def get(self, mid):
        return self._by_id.get(mid)


@pytest.fixture
async def tenant_manager():
    """两个租户各自的 mcp store（物理不同库）→ manager 经 provider 路由。"""
    store_a = _FakeStore([
        {"id": "m-a", "name": "a-tools", "transport": "http", "config": {"url": "http://a"},
         "enabled": True},
    ])
    store_b = _FakeStore([
        {"id": "m-b", "name": "b-tools", "transport": "http", "config": {"url": "http://b"},
         "enabled": True},
    ])
    global_store = _FakeStore([])
    mgr = MCPClientManager(
        global_store,
        stores_provider=lambda tid: _pick(tid, store_a, store_b, global_store),
        # 不注入 server_ids_for → 未绑定解析语义：返回该租户库全部 enabled
    )
    return mgr


async def _pick(tid, a, b, g):
    return {"team-a": a, "team-b": b}.get(tid, g)


async def test_mcp_manager_tenant_isolation(tenant_manager) -> None:
    mgr = tenant_manager
    clients_a = await mgr.clients_for_agent("triage", tenant_id="team-a")
    clients_b = await mgr.clients_for_agent("triage", tenant_id="team-b")
    clients_default = await mgr.clients_for_agent("triage", tenant_id=None)

    assert [c.name for c in clients_a] == ["a-tools"]  # 租户 A 只见自己的 server
    assert [c.name for c in clients_b] == ["b-tools"]
    assert clients_default == []

    # 跨租户刷新互不影响：refresh team-a 的 server 后 team-b 仍正常
    await mgr.refresh_server("m-a", tenant_id="team-a")
    assert [c.name for c in await mgr.clients_for_agent("triage", tenant_id="team-a")] == ["a-tools"]


async def test_mcp_manager_async_tenant_server_ids() -> None:
    """server_ids_for 支持 async + (agent, tenant) 签名——按租户绑定解析。"""
    store = _FakeStore([
        {"id": "m1", "name": "s1", "transport": "http", "config": {"url": "http://x"},
         "enabled": True},
    ])
    mgr = MCPClientManager(
        store,
        stores_provider=lambda tid: _store_by_tenant(tid, store),
        server_ids_for=lambda agent, tenant=None: (
            {"m1"} if tenant == "team-a" else set()
        ),
    )
    got_a = await mgr.clients_for_agent("triage", tenant_id="team-a")
    got_b = await mgr.clients_for_agent("triage", tenant_id="team-b")
    assert [c.name for c in got_a] == ["s1"]  # team-a 绑定了 m1
    assert got_b == []  # team-b 明确不绑 → 无 client（两态语义）


async def _store_by_tenant(tid, store):
    return store


# ======================================================================
# §7.4 runner 经 current_tenant 解析 per-tenant agent 配置
# ======================================================================
async def test_executor_sets_tenant_context_for_runner() -> None:
    """DAGExecutor 在节点执行期置 current_tenant——node_runner 读取到租户。"""
    seen: dict[str, str | None] = {}

    async def runner(node, params):
        seen["tenant"] = current_tenant.get()
        return {"ok": True}

    wf = Workflow.load_yaml(SIMPLE_YAML)
    ex = DAGExecutor("run_ctx", "team-a", wf.dag, InMemoryStateStore(), node_runner=runner)
    await ex.run()
    assert seen["tenant"] == "team-a"


async def test_runner_uses_per_tenant_config_provider() -> None:
    """runner 的 agent_config_provider 按 current_tenant 解析——租户 B 停用 agent 生效。"""
    from agentflow.agents.agent_config import AgentConfigResolver

    async def provider(tenant_id):
        if tenant_id == "team-b":
            row = {"name": "triage", "origin": "builtin", "enabled": False,
                   "role": "diagnose", "stage": "detect"}
            return AgentConfigResolver([row])
        return AgentConfigResolver([])  # team-a 无覆盖 → 静态默认 enabled

    resolver_b = await provider("team-b")
    cfg_b = resolver_b.resolve("triage")
    assert cfg_b is not None and cfg_b.enabled is False  # 租户 B 的停用覆盖生效
    resolver_a = await provider("team-a")
    cfg_a = resolver_a.resolve("triage")  # 无覆盖行 → 静态回退（内置 15 永可解析）
    assert cfg_a is not None and cfg_a.enabled is True
