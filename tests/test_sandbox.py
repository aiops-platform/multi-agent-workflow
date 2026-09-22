"""M4：sandbox 模块 —— exec 服务限制 / ToolPolicy / ActionExecutor 白名单。"""
from __future__ import annotations

import pytest

from agentflow.sandbox.action_executor import ActionExecutor, ActionValidationError, _parse_quantity
from agentflow.sandbox.exec_service import (
    ExecRequest,
    PythonRequest,
    WriteRequest,
    exec_cmd,
    exec_python,
    write_file,
)
from agentflow.sandbox.policy import ToolPolicy


# ======================================================================
# exec_service：§10.2 限制
# ======================================================================
async def test_exec_cmd_basic() -> None:
    r = await exec_cmd(ExecRequest(cmd="echo hello && echo err >&2"))
    assert r["rc"] == 0
    assert "hello" in r["stdout"]
    assert "err" in r["stderr"]


async def test_exec_python() -> None:
    r = await exec_python(PythonRequest(code="print(6*7)"))
    assert r["rc"] == 0
    assert "42" in r["stdout"]


async def test_write_file_allowed_in_allowlist() -> None:
    # 白名单 /tmp 下可写
    import tempfile

    d = tempfile.mkdtemp(prefix="sbx_")
    r = await write_file(WriteRequest(path=f"/tmp/{d.split('/')[-1]}/a.txt", content="hi"))
    assert r["written"] is True


async def test_exec_cmd_timeout() -> None:
    r = await exec_cmd(ExecRequest(cmd="sleep 5", timeout=1))
    assert r["timed_out"] is True


# ======================================================================
# ToolPolicy：§9.5 deny 优先 / 兜底 DENY
# ======================================================================
def test_policy_deny_precedence() -> None:
    """§9.5 的四条判据：deny 优先 / allow / agent 未注册 / 兜底 DENY。

    ⚠️ 本用例钉的是**意图语义**，不是运行期行为：`ToolPolicy` 目前**没有任何运行期
    消费方**（真正生效的是 `scopes.build_permission_context`，它只生成 allow、不读这里），
    所以这些断言**全绿也不代表租户 deny 规则生效了**。见 `docs/TODO.md` §23.3。
    """
    p = ToolPolicy()
    # ① deny 优先：team-alpha deny 了写动作
    assert p.decide(tool_name="scale_deployment", agent="infra-remediator", tenant_id="team-alpha") == "DENY"
    # ② allow：本地诊断工具、且该 agent 已注册
    assert p.decide(tool_name="search_knowledge", agent="root-cause", tenant_id="team-alpha") == "ALLOW"
    # ③ agent 未注册该工具 → DENY
    assert p.decide(tool_name="search_knowledge", agent="tester", tenant_id="team-alpha") == "DENY"
    # ④ 未知工具 → DENY
    assert p.decide(tool_name="unknown_tool", agent="triage", tenant_id="team-alpha") == "DENY"


def test_policy_denies_data_plane_tools_not_in_local_registry() -> None:
    """数据面工具**已迁 MCP**，不在这里的注册表里 —— 因此被兜底判 DENY。

    这条是**刻意钉住的设计决策**，不是缺陷：`policy.py` 的默认租户配置里已写明
    「数据源与 CMDB 工具已迁 MCP（design-v5.6）——其放行由 MCP 侧 readOnlyHint +
    allow_extra 承担，不在此处枚举」。本用例原先断言 `query_logs` → ALLOW，
    那是批次 3 删除直连实现**之前**的事实，测试未同步（陈旧用例，见 `docs/TODO.md` §10）。

    ⚠️ 它同时暴露一个真实边界：**`ToolPolicy` 对 MCP 工具一无所知** —— 即使它将来
    接上运行期，也只管得住本地 registry 里的东西，MCP 侧的放行是另一条路径。
    """
    p = ToolPolicy()
    for tool in ("query_logs", "query_metrics", "get_service_topology"):
        assert p.decide(tool_name=tool, agent="log-analyst", tenant_id="team-alpha") == "DENY"


def test_policy_allowed_tools() -> None:
    p = ToolPolicy()
    tools = p.allowed_tools("fix-implementer", "team-alpha")
    assert "sandbox_run_python" in tools
    assert "scale_deployment" not in tools  # deny


def test_policy_resource_limit() -> None:
    p = ToolPolicy()
    lim = p.resource_limit("sandbox_run_shell", "team-alpha")
    assert lim["timeout"] == 300
    assert lim["result_limit"] == 1_000_000  # Tool Registry 默认 1MB


# ======================================================================
# ActionExecutor：§10.3 参数白名单
# ======================================================================
def test_parse_quantity() -> None:
    assert _parse_quantity("100m") == 100
    assert _parse_quantity("4") == 4000
    assert _parse_quantity("2Gi") == 2048
    assert _parse_quantity("512Mi") == 512


async def test_action_scale_replicas_range() -> None:
    ex = ActionExecutor(namespace_whitelist=["order"])
    with pytest.raises(ActionValidationError, match="范围"):
        await ex.scale_deployment(namespace="order", name="order-service", replicas=11)


async def test_action_unsupported() -> None:
    ex = ActionExecutor()
    with pytest.raises(ActionValidationError, match="不支持的动作"):
        await ex.execute("drop_database", namespace="order")


async def test_action_delete_temp_path_whitelist() -> None:
    ex = ActionExecutor(namespace_whitelist=["order"])
    with pytest.raises(ActionValidationError, match="路径必须在"):
        await ex.delete_temp_file(namespace="order", path="/etc/passwd")


async def test_action_patch_resources_range() -> None:
    ex = ActionExecutor(namespace_whitelist=["order"])
    with pytest.raises(ActionValidationError, match="超出范围"):
        await ex.patch_resources(namespace="order", name="order-service", cpu="16", memory="1Gi")


async def test_action_namespace_whitelist() -> None:
    ex = ActionExecutor(namespace_whitelist=["order"])
    with pytest.raises(ActionValidationError, match="白名单"):
        await ex.scale_deployment(namespace="kube-system", name="x", replicas=2)


# ======================================================================
# L2 工具接线：build_toolkit 注入沙箱/动作执行器（§4.1 / §10.3）
# ======================================================================
class _FakeSandboxClient:
    """返回固定结果的假 SandboxClient（无需真实沙箱）。"""

    async def run_python(self, code, timeout=300):
        from agentflow.sandbox.client import SandboxResult

        return SandboxResult(rc=0, stdout=f"ran: {code[:20]}", stderr="")

    async def run_shell(self, cmd, timeout=300):
        from agentflow.sandbox.client import SandboxResult

        return SandboxResult(rc=0, stdout=f"shell: {cmd[:20]}", stderr="")

    async def write_file(self, path, content):
        return {"written": True, "path": path, "bytes": len(content)}


async def test_build_toolkit_includes_l2_tools() -> None:
    from agentflow.agents.mcp import build_toolkit

    tk = build_toolkit("fix-implementer", sandbox_client=_FakeSandboxClient())
    schemas = await tk.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "sandbox_run_python" in names
    assert "sandbox_run_shell" in names
    assert "sandbox_write_file" in names

    # 调用 L2 工具函数 → 走假沙箱
    from agentflow.agents.tools import build_l2_tools

    l2 = {t["name"]: t["func"] for t in build_l2_tools("fix-implementer", sandbox_client=_FakeSandboxClient())}
    out = await l2["sandbox_run_python"](code="print(1)")
    assert "ran:" in out["stdout"]
    out2 = await l2["sandbox_write_file"](path="/workspace/a.py", content="x=1")
    assert out2["written"] is True


async def test_build_toolkit_l2_absent_without_executor() -> None:
    """未接沙箱时 L2 工具不注入（避免 agent 拿到不可用的执行工具）。

    判据是「**没有 `sandbox_client` 参数**」——不是 `use_mock`。那个参数随批次 3
    删除直连实现一同消失（数据面改走 MCP），用例未同步而一直红（见 `docs/TODO.md` §10）。
    """
    from agentflow.agents.mcp import build_toolkit

    tk = build_toolkit("fix-implementer")  # 无 sandbox_client
    schemas = await tk.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "sandbox_run_python" not in names


# ======================================================================
# SandboxClient：把"沙箱回的 200"翻译成**正确的成功/失败**
# ======================================================================
async def test_client_write_file_rejects_silent_refusal() -> None:
    """exec 服务的**拒写也是 HTTP 200**（`{"written": false, "error": …}`）。

    只看状态码会把"被拒"读成"成功"：`ws_write_file` 于是回一句「新建 X（沙箱）」，
    而工作区里什么都没有 —— 又一条**静默成功**，且它不需要任何环境错配，只要工作区根
    不在 `SBX_WRITABLE` 里就会发生（实测 2026-09-22 的 run_fc9e158b55 就是这条链）。
    判据必须单边：**只认 `written` 为 true**。
    """
    import httpx

    from agentflow.errors import DataSourceError
    from agentflow.sandbox.client import SandboxClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/write"
        return httpx.Response(200, json={"written": False, "error": "路径不在可写白名单: /x"})

    c = SandboxClient("http://127.0.0.1:1")
    await c._client.aclose()   # 换成假传输（httpx 标准姿势）：本用例不该真的连沙箱
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(DataSourceError, match="路径不在可写白名单"):
            await c.write_file("/x", "y")
    finally:
        await c.aclose()


async def test_client_write_file_passes_through_on_success() -> None:
    """负向对照：真的写了（`written: true`）→ 原样返回，不误报。"""
    import httpx

    from agentflow.sandbox.client import SandboxClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"written": True, "path": "/x", "bytes": 1})

    c = SandboxClient("http://127.0.0.1:1")
    await c._client.aclose()
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert (await c.write_file("/x", "y"))["written"] is True
    finally:
        await c.aclose()


def test_client_does_not_use_env_proxy() -> None:
    """沙箱是 loopback sidecar，**不该走代理**（`trust_env=False`）。

    httpx 默认 `trust_env=True`，会认 `http_proxy`/`all_proxy`，而且**不像 urllib
    那样跳过 loopback** —— 于是同一条 URL 在 worker 侧经代理、在 `_env_preflight`
    （走 urllib）侧直连，**两处结论可以相反**。实测（2026-09-22，本机有
    `http_proxy=127.0.0.1:7890`）：经代理打不通的端口得到**代理的 502**，直连得到
    `httpx.ReadError`（其 `str()` 是空串，会把错误文案的原因吞掉）。
    """
    from agentflow.sandbox.client import SandboxClient

    assert SandboxClient("http://127.0.0.1:1")._client.trust_env is False
