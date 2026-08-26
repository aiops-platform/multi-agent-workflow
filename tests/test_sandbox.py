# -*- coding: utf-8 -*-
"""M4：sandbox 模块 —— exec 服务限制 / ToolPolicy / ActionExecutor 白名单。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentflow.sandbox.action_executor import ActionValidationError, ActionExecutor, _parse_quantity
from agentflow.sandbox.exec_service import ExecRequest, PythonRequest, WriteRequest, exec_cmd, exec_python, write_file
from agentflow.sandbox.policy import TenantToolConfig, ToolPolicy


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
    p = ToolPolicy()
    # team-alpha deny 了写动作
    assert p.decide(tool_name="scale_deployment", agent="infra-remediator", tenant_id="team-alpha") == "DENY"
    # 诊断工具 allow
    assert p.decide(tool_name="query_logs", agent="log-analyst", tenant_id="team-alpha") == "ALLOW"
    # agent 未注册该工具 → DENY
    assert p.decide(tool_name="query_logs", agent="tester", tenant_id="team-alpha") == "DENY"
    # 未知工具 → DENY
    assert p.decide(tool_name="unknown_tool", agent="triage", tenant_id="team-alpha") == "DENY"


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

    tk = build_toolkit("fix-implementer", use_mock=True, sandbox_client=_FakeSandboxClient())
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
    """未接沙箱时 L2 工具不注入（避免 agent 拿到不可用的执行工具）。"""
    from agentflow.agents.mcp import build_toolkit

    tk = build_toolkit("fix-implementer", use_mock=True)  # 无 sandbox_client
    schemas = await tk.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "sandbox_run_python" not in names
