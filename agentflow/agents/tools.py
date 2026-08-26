# -*- coding: utf-8 -*-
"""工具治理（design §7 Tool Registry + §10.4 Tool Governance）。

L1：只读工具（Agent 容器内本地执行 / 数据源 MCP）。
L2：代码/Shell/基础设施动作（经 gRPC 到沙箱 Pod / Action Executor 执行，M4 接入）。

Tool Registry 定义：agent 可见性 / 超时 / 限流 / 结果上限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    # ---- 数据源 MCP 只读工具（§10.4）----
    "query_logs": ToolSpec(
        "query_logs",
        ["log-analyst", "trace-analyst", "root-cause"],
        timeout=30, rate_limit=100, result_limit=1_000_000,
        level="L1", description="查询日志（service, level）",
    ),
    "get_trace": ToolSpec(
        "get_trace",
        ["trace-analyst", "root-cause", "triage"],
        timeout=30, rate_limit=60,
        level="L1", description="查询链路追踪",
    ),
    "query_metrics": ToolSpec(
        "query_metrics",
        ["metrics-analyst", "root-cause"],
        timeout=30, rate_limit=60,
        level="L1", description="查询 Prometheus 指标",
    ),
    "check_infra": ToolSpec(
        "check_infra",
        ["infra-locator", "root-cause"],
        timeout=10, rate_limit=60,
        level="L1", description="查询 K8s 资源状态",
    ),
    "describe_pod": ToolSpec(
        "describe_pod",
        ["infra-locator", "root-cause"],
        timeout=10,
        level="L1", description="describe pod",
    ),
    "locate_code": ToolSpec(
        "locate_code",
        ["code-locator", "root-cause"],
        timeout=30,
        level="L1", description="CMDB 查询 service→repo 映射",
    ),
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
# L1 mock 实现（本地联调用，testbed 就绪前提供确定性数据）
# ======================================================================
async def _mock_query_logs(service: str, level: str = "ERROR", **_: Any) -> dict:
    return {
        "service": service,
        "level": level,
        "logs": [
            {"ts": "2026-08-19T14:31:00+08:00", "level": "ERROR",
             "msg": "java.io.IOException: No space left on device"},
        ],
    }


async def _mock_get_trace(trace_id: str, **_: Any) -> dict:
    return {
        "trace_id": trace_id,
        "spans": [
            {"service": "order-service", "status": "ERROR", "duration_ms": 3200},
            {"service": "warranty-service", "status": "ERROR", "duration_ms": 2900},
        ],
        "failing_service": "warranty-service",
        "first_error": "java.lang.IllegalStateException: fin must not be null",
    }


async def _mock_query_metrics(service: str, metric: str, **_: Any) -> dict:
    return {"service": service, "metric": metric, "value": 100.0, "unit": "percent"}


async def _mock_check_infra(namespace: str, pod: str | None = None, **_: Any) -> dict:
    return {"namespace": namespace, "pod": pod, "status": "CrashLoopBackOff", "restarts": 5}


async def _mock_describe_pod(namespace: str, pod: str, **_: Any) -> dict:
    return {"namespace": namespace, "pod": pod, "status": "CrashLoopBackOff", "events": []}


async def _mock_locate_code(service: str, **_: Any) -> dict:
    return {"service": service, "repo_url": f"https://github.com/company/{service}", "base_sha": "abc123"}


async def _cmdb_locate_code(cmdb, service: str, **_: Any) -> dict:
    """CMDB 驱动的 locate_code（design §9.4）：service → RepoSpec。"""
    spec = await cmdb.get_repo_for_service(service)
    if spec is None:
        return {
            "service": service, "found": False,
            "summary": f"CMDB 未找到 {service} 对应的 repo（可能未纳管）",
        }
    return {
        "service": service, "found": True,
        "repo_url": spec.url, "base_sha": spec.base_sha,
        "suspicious_files": [],
        "summary": f"CMDB 定位 {service} → {spec.url}",
    }


async def _mock_search_knowledge(query: str, **_: Any) -> dict:
    return {"found": True, "similar_incidents": ["INC0001"], "suggested_actions": []}


MOCK_L1_TOOLS: dict[str, Any] = {
    "query_logs": _mock_query_logs,
    "get_trace": _mock_get_trace,
    "query_metrics": _mock_query_metrics,
    "check_infra": _mock_check_infra,
    "describe_pod": _mock_describe_pod,
    "locate_code": _mock_locate_code,
    "search_knowledge": _mock_search_knowledge,
}


def build_l1_tools(agent_name: str, *, use_mock: bool = True, cmdb=None, datasource=None) -> list[dict]:
    """为 agent 生成 L1 工具列表（AgentScope FunctionTool 形态）。

    - ``use_mock=True``：绑定 mock 实现（本地联调 / 无数据源时的回退）。
    - 传 ``cmdb``（TenantMappingProvider）：``locate_code`` 走 CMDB 查询（§9.4）。
    - 传 ``datasource``（RealDataSourceAdapter）：``query_logs``/``query_metrics``/
      ``check_infra``/``describe_pod`` 绑定真实数据源（testbed 联调），工具签名不变。
    """
    tools = []
    for spec in tools_for_agent(agent_name):
        if spec.level != "L1":
            continue
        func = MOCK_L1_TOOLS.get(spec.name)
        if spec.name == "locate_code" and cmdb is not None:
            from functools import partial

            func = partial(_cmdb_locate_code, cmdb)
        elif datasource is not None and hasattr(datasource, spec.name):
            from functools import partial

            func = partial(getattr(datasource, spec.name))
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": {"type": "object", "properties": {}},
            "func": func,
        })
    return tools
