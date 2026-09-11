"""工具治理（design §7 Tool Registry + §10.4 Tool Governance）。

L1：只读工具（Agent 容器内本地执行 / 数据源 MCP）。
L2：代码/Shell/基础设施动作（经 gRPC 到沙箱 Pod / Action Executor 执行，M4 接入）。

Tool Registry 定义：agent 可见性 / 超时 / 限流 / 结果上限。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolSpec:
    name: str
    agents: list[str]
    timeout: int = 30
    rate_limit: int = 60
    result_limit: int = 1_000_000
    level: str = "L1"  # L1 只读 | L2 执行
    needs_approval: bool = False
    description: str = ""


TOOL_REGISTRY: dict[str, ToolSpec] = {
    # ---- 数据源查询工具：**已迁至 MCP**（design-v5.5）----
    # query_logs / get_trace / query_metrics / check_infra / describe_pod 不再在这里
    # 注册——它们由 `aiops-datasource-mcp-server` 提供，经 agent_configs.mcp_server_ids
    # 绑定（agent 侧名为 mcp__<server>__<tool>），权限经 allow_extra 下发。
    # 见 design-v5.5 §3/§5 与 skill §批3。
    #
    # ---- 本地只读工具（非数据源：知识检索）----
    # locate_code 已迁至 MCP（locate_repo / get_service_topology）——design-v5.5。
    "search_knowledge": ToolSpec(
        "search_knowledge",
        ["knowledge-lookup", "root-cause"],
        timeout=30,
        level="L1", description="知识图谱检索",
    ),
    # ---- L2 执行工具（M4 接入沙箱 / Action Executor）----
    "sandbox_run_shell": ToolSpec(
        "sandbox_run_shell",
        ["fix-implementer", "tester"],
        timeout=300, rate_limit=10, result_limit=1_000_000,
        level="L2", description="沙箱内执行 shell",
    ),
    "sandbox_run_python": ToolSpec(
        "sandbox_run_python",
        ["fix-implementer", "tester"],
        timeout=300, rate_limit=10,
        level="L2", description="沙箱内执行 python",
    ),
    "sandbox_write_file": ToolSpec(
        "sandbox_write_file",
        ["fix-implementer"],
        timeout=60,
        level="L2", description="沙箱内写文件（writable_allowlist 内）",
    ),
    "scale_deployment": ToolSpec(
        "scale_deployment",
        ["infra-remediator"],
        timeout=60, needs_approval=True,
        level="L2", description="扩缩容 Deployment（参数白名单）",
    ),
    "restart_pod": ToolSpec(
        "restart_pod",
        ["infra-remediator"],
        timeout=60, needs_approval=True,
        level="L2", description="重启 Pod（白名单）",
    ),
    # ---- 工作区工具（修复侧；§8.7 真实读写代码，实现见 workspace_tools.py）----
    # 路径/命令白名单在 workspace_tools 内强制（工作区前缀 + git 子命令白名单）。
    "ws_read_file": ToolSpec(
        "ws_read_file", ["fix-implementer", "tester", "reviewer", "code-locator"],
        timeout=30, level="L1", description="读本次 run 工作区内文件",
    ),
    "ws_list_files": ToolSpec(
        "ws_list_files", ["fix-implementer", "tester", "reviewer", "code-locator"],
        timeout=30, level="L1", description="列出工作区内文件",
    ),
    "ws_write_file": ToolSpec(
        "ws_write_file", ["fix-implementer"],
        timeout=60, needs_approval=True, level="L2",
        description="写工作区内文件（越界拒绝）",
    ),
    "ws_run_tests": ToolSpec(
        "ws_run_tests", ["tester", "fix-implementer"],
        timeout=300, level="L2", description="工作区内执行测试命令（白名单前缀）",
    ),
    "ws_git": ToolSpec(
        "ws_git", ["committer", "fix-implementer"],
        timeout=120, needs_approval=True, level="L2",
        description="工作区内 git 操作（子命令白名单，无 pull/fetch/reset）",
    ),
    "patch_resources": ToolSpec(
        "patch_resources",
        ["infra-remediator"],
        timeout=60, needs_approval=True,
        level="L2", description="调整资源配额（范围受限）",
    ),
}


def tools_for_agent(agent_name: str) -> list[ToolSpec]:
    return [spec for spec in TOOL_REGISTRY.values() if agent_name in spec.agents]


# ======================================================================
# L2 工具（经 SandboxClient / ActionExecutor 执行，design §4.1 / §10.3）
# ======================================================================
async def _l2_sandbox_run_python(sandbox_client, code: str, timeout: int = 300) -> dict:
    r = await sandbox_client.run_python(code, timeout=timeout)
    return {"rc": r.rc, "stdout": r.stdout, "stderr": r.stderr, "timed_out": r.timed_out}


async def _l2_sandbox_run_shell(sandbox_client, cmd: str, timeout: int = 300) -> dict:
    r = await sandbox_client.run_shell(cmd, timeout=timeout)
    return {"rc": r.rc, "stdout": r.stdout, "stderr": r.stderr, "timed_out": r.timed_out}


async def _l2_sandbox_write_file(sandbox_client, path: str, content: str) -> dict:
    return await sandbox_client.write_file(path, content)


async def _l2_action(sandbox_action_executor, action: str, namespace: str, **params) -> dict:
    return await sandbox_action_executor.execute(action, namespace=namespace, **params)


def build_l2_tools(agent_name: str, *, sandbox_client=None, action_executor=None) -> list[dict]:
    """为 agent 生成 L2 执行工具（沙箱 gRPC/HTTP + ActionExecutor，§4.1/§10.3）。

    工具名与 Tool Registry 一致；未提供对应执行器时返回空（联调未接沙箱时 L2 不可用）。
    """
    from functools import partial

    tools = []
    for spec in tools_for_agent(agent_name):
        if spec.level != "L2":
            continue
        if spec.name in ("sandbox_run_python", "sandbox_run_shell", "sandbox_write_file") and sandbox_client is not None:
            handler = {
                "sandbox_run_python": _l2_sandbox_run_python,
                "sandbox_run_shell": _l2_sandbox_run_shell,
                "sandbox_write_file": _l2_sandbox_write_file,
            }[spec.name]
            tools.append({"name": spec.name, "description": spec.description,
                          "parameters": {"type": "object", "properties": {}},
                          "func": partial(handler, sandbox_client)})
        elif spec.name in ("scale_deployment", "restart_pod", "patch_resources") and action_executor is not None:
            tools.append({"name": spec.name, "description": spec.description,
                          "parameters": {"type": "object", "properties": {}},
                          "func": partial(_l2_action, action_executor, spec.name)})
    return tools


# ======================================================================
# 本地只读工具实现（非数据源：CMDB 映射 / 知识检索）
# ======================================================================
# 数据源查询（日志/指标/K8s）已迁至 MCP（design-v5.5），此处不再有对应 mock——
# 留着会让人以为"还有一条能用的数据路径"，而它其实只返回编造的数据。
async def _mock_search_knowledge(query: str, **_: Any) -> dict:
    """知识检索占位实现。

    ⚠️ 恒返回同一批虚构条目（INC0001）——**不是真实检索结果**。真实后端按
    design-v5.4 §7.3 应走租户 MCP（租户自建暴露 search_knowledge 的 server 并绑定）。
    在此之前，agent 引用它的结论时应知道这是占位数据。
    """
    return {"found": True, "similar_incidents": ["INC0001"], "suggested_actions": []}


LOCAL_TOOLS: dict[str, Any] = {
    "search_knowledge": _mock_search_knowledge,
}


def build_local_tools(agent_name: str) -> list[dict]:
    """为 agent 生成**本地**只读工具（AgentScope FunctionTool 形态）。

    仅剩 ``search_knowledge``（占位实现，见其 docstring）。

    其余能力均已迁至 MCP——数据查询（日志/指标/K8s，design-v5.5 批 1-3）与
    CMDB（``locate_repo`` / ``get_service_topology``）。
    """
    tools = []
    for spec in tools_for_agent(agent_name):
        if spec.level != "L1":
            continue
        func = LOCAL_TOOLS.get(spec.name)
        if func is None:
            continue  # 注册表里的 L1 工具都应在此有实现；无实现的直接跳过
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": {"type": "object", "properties": {}},
            "func": func,
        })
    return tools


def build_workspace_tools(agent_name: str) -> list[dict]:
    """为 agent 生成工作区工具（§8.7；实现见 ``workspace_tools.py``）。

    与 L1/L2 并列的第三类：不经数据源、也不经沙箱，直接读写**本次 run 的工作区**
    （定位靠 ``exec_context.current_run``，不接受 LLM 传 run/service 之外的越界参数）。
    ``ws_read_file``/``ws_list_files`` 只读（DONT_ASK 下自动 ALLOW），
    写/git/测试走 needs_approval，由租户 ToolPolicy 兜底。
    """
    from .workspace_tools import WORKSPACE_TOOLS

    out: list[dict] = []
    for spec in tools_for_agent(agent_name):
        func = WORKSPACE_TOOLS.get(spec.name)
        if func is None:
            continue
        out.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": {"type": "object", "properties": {}},
            "func": func,
            "read_only": spec.level == "L1",
        })
    return out
