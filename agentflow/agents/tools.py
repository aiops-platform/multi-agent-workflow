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


def build_l1_tools(agent_name: str, *, use_mock: bool = True) -> list[dict]:
    """为 agent 生成 L1 工具列表（AgentScope FunctionTool 形态）。

    ``use_mock=True`` 时绑定 mock 实现（本地联调 / 无数据源时的回退）。
    数据源就绪后切换为 MCP 工具（mcp.py）。
    """
    tools = []
    for spec in tools_for_agent(agent_name):
        if spec.level != "L1":
            continue
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": {"type": "object", "properties": {}},
            "func": MOCK_L1_TOOLS.get(spec.name),
        })
    return tools
