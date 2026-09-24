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
    # ---- 数据源查询工具：**已迁至 MCP**（design-v5.6）----
    # query_logs / get_trace / query_metrics / check_infra / describe_pod 不再在这里
    # 注册——它们由 `aiops-datasource-mcp-server` 提供，经 agent_configs.mcp_server_ids
    # 绑定（agent 侧名为 mcp__<server>__<tool>），权限经 allow_extra 下发。
    # 见 design-v5.6 §3.2/§3.4 与 skill §批3。
    #
    # ---- 本地只读工具（非数据源：知识检索）----
    # locate_code 已迁至 MCP（locate_repo / get_service_topology）——design-v5.6。
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
        timeout=300, level="L2",
        # 描述要如实：命令**由部署配置给定**，调用方不能传。旧文案"白名单前缀"会让
        # 模型以为自己能指定命令——而那个"白名单"里含 `bash `，等于没有。
        description="执行该服务在部署配置中登记的测试命令（命令固定，不接受传参）",
    ),
    "ws_git": ToolSpec(
        "ws_git", ["committer", "fix-implementer"],
        timeout=120, needs_approval=True, level="L2",
        description="工作区内 git 操作（子命令白名单，无 pull/fetch/reset）",
    ),
    "ws_open_pr": ToolSpec(
        "ws_open_pr", ["committer"],
        timeout=120, needs_approval=True, level="L2",
        # 「推分支」与「开 PR」收成一个工具：`ticket-done` 判"已交付"靠的是 pr_url，
        # 而"能推、但不能开 PR"会留下一个**系统性为空**的字段（见 workspace_tools.ws_open_pr）。
        description="把本次 run 的分支推到远端并开 PR（base 取仓库默认分支；已存在则复用）",
    ),
    "patch_resources": ToolSpec(
        "patch_resources",
        ["infra-remediator"],
        timeout=60, needs_approval=True,
        level="L2", description="调整资源配额（范围受限）",
    ),
    "ws_build_artifact": ToolSpec(
        "ws_build_artifact", ["ci-builder"],
        timeout=300, level="L2",
        # 描述要如实：命令**由部署配置给定**（AGENTFLOW_BUILD_CMDS），调用方不能传。
        description="在本次 run 的工作区编译打包（命令固定，不接受传参）",
    ),
    # ---- 发布工具（实现见 release_tools.py）----
    # `ws_merge_pr` 是本仓第一个**动 git 主干**的工具：不可逆，所以合并方式固定 squash、
    # 校验与幂等回落都在实现里（见 release_tools.ws_merge_pr 的 docstring）。
    "ws_merge_pr": ToolSpec(
        "ws_merge_pr", ["merger"],
        timeout=120, needs_approval=True, level="L2",
        description="把本次 run 的 PR 合并到主干（squash；已合并则复用既有结果）",
    ),
    # 与 `ws_build_artifact` 同属 ci 节点，但**这个进不了沙箱**（要 docker daemon/socket）
    # —— 所以它不叫 ws_*，走的是 release_tools 那条宿主侧的线。
    "ws_build_image": ToolSpec(
        "ws_build_image", ["ci-builder"],
        timeout=900, level="L2",
        description="把工作区构建成镜像，tag = <service>:<主干合并提交前 12 位>",
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
# 数据源查询（日志/指标/K8s）已迁至 MCP（design-v5.6），此处不再有对应 mock——
# 留着会让人以为"还有一条能用的数据路径"，而它其实只返回编造的数据。
async def _mock_search_knowledge(query: str, **_: Any) -> dict:
    """知识检索占位实现。

    ⚠️ 恒返回同一批虚构条目（INC0001）——**不是真实检索结果**。真实后端按
    design-v5.6 §4.7.3 应走租户 MCP（租户自建暴露 search_knowledge 的 server 并绑定）。
    在此之前，agent 引用它的结论时应知道这是占位数据。
    """
    return {"found": True, "similar_incidents": ["INC0001"], "suggested_actions": []}


LOCAL_TOOLS: dict[str, Any] = {
    "search_knowledge": _mock_search_knowledge,
}


def build_local_tools(agent_name: str) -> list[dict]:
    """为 agent 生成**本地**只读工具（AgentScope FunctionTool 形态）。

    仅剩 ``search_knowledge``（占位实现，见其 docstring）。

    其余能力均已迁至 MCP——数据查询（日志/指标/K8s，design-v5.6 批 1-3）与
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


#: 必须经沙箱的工作区工具。判据是**它会不会执行/改动仓库里的东西**：
#: 写文件与跑测试都在执行仓库代码（`build.gradle` 本身就是脚本），而 worker 持有
#: DeepSeek key / DB DSN / MCP 凭证 / git PAT —— 谁持有密钥，谁不执行不可信代码。
#:
#: **读**（ws_read_file / ws_list_files）刻意不在列：诊断链的 `code-locator` 靠它们
#: 定位，如果读也依赖沙箱，沙箱一挂整条诊断链就跑不起来。读不改状态、不执行仓库
#: 代码，风险低。这个取舍是有意的，别"顺手统一"。
WORKSPACE_SANDBOXED = frozenset({"ws_write_file", "ws_run_tests", "ws_build_artifact"})


def build_release_tools(agent_name: str) -> list[dict]:
    """为 agent 生成发布工具（实现见 ``release_tools.py``）。

    与 ``build_workspace_tools`` 同一形状，但**不需要注入任何执行器** ——
    它跑的是 worker 本机的 ``gh``，凭证由 ``gh`` 自己从 keychain 取（§9.7）。

    ⚠️ 这个函数必须被 ``agents/mcp.py:_build_function_tools`` 调用到。只往
    ``TOOL_REGISTRY`` 里加一条 spec 是**不够**的：那样 ``build_permission_context``
    照旧会发 allow 规则，而模型**看不到工具** —— 症状是烧完轮次报"未输出合法 JSON"，
    与工具根本不存在一模一样。`tests/test_agents.py` 有一条不变量测试守这件事。
    """
    from .release_tools import RELEASE_TOOLS

    out: list[dict] = []
    for spec in tools_for_agent(agent_name):
        func = RELEASE_TOOLS.get(spec.name)
        if func is None:
            continue
        out.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": {"type": "object", "properties": {}},
            "func": func,
            "read_only": False,  # 它改的是远端仓库，不是只读
        })
    return out


def build_workspace_tools(agent_name: str, *, sandbox_client=None) -> list[dict]:
    """为 agent 生成工作区工具（§8.7；实现见 ``workspace_tools.py``）。

    与 L1/L2 并列的第三类：不经数据源，直接读写**本次 run 的工作区**
    （定位靠 ``exec_context.current_run``，不接受 LLM 传 run/service 之外的越界参数）。
    ``ws_read_file``/``ws_list_files`` 只读（DONT_ASK 下自动 ALLOW），
    写/git/测试走 needs_approval，由租户 ToolPolicy 兜底。

    ``sandbox_client``：注入后 ``WORKSPACE_SANDBOXED`` 里的工具改走沙箱 sidecar
    （照 ``build_l2_tools`` 的 ``partial`` 模式）。**未注入时不静默降级为本地执行**，
    而是注册一个调用即报错的占位——见 ``_fail_closed_ws_tool``。
    """
    from .workspace_tools import (
        WORKSPACE_TOOLS,
        _fail_closed_ws_tool,
        ws_build_artifact_sandboxed,
        ws_run_tests_sandboxed,
        ws_write_file_sandboxed,
    )

    sandboxed = {
        "ws_write_file": ws_write_file_sandboxed,
        "ws_run_tests": ws_run_tests_sandboxed,
        "ws_build_artifact": ws_build_artifact_sandboxed,
    }

    out: list[dict] = []
    for spec in tools_for_agent(agent_name):
        func = WORKSPACE_TOOLS.get(spec.name)
        if func is None:
            continue
        if spec.name in WORKSPACE_SANDBOXED:
            if sandbox_client is not None:
                from functools import partial

                func = partial(sandboxed[spec.name], sandbox_client)
            else:
                func = _fail_closed_ws_tool(spec.name, func)
        out.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": {"type": "object", "properties": {}},
            "func": func,
            "read_only": spec.level == "L1",
        })
    return out
