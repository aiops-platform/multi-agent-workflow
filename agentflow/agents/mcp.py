"""Toolkit 组装（design §5 AgentScope 原生支持 / §7 数据源 / MCP Server 配置共存）。

集成模式已在 spike 验证：
- ``spike/s-002-agent-scope-mcp/mcp_adapter_prototype.py``：真实模型自动发现并调用
  MCP 工具（Toolkit(tools=[], mcps=[client])，见 S-002 报告）。
- ``spike/s-011-agent-smoke/mock_mcp_server.py``：mock MCP server（供 spike 期验证）。
- ``scripts/mock_mcp_server.py``：本仓库 MCP 配置页用的 mock server（mcp v1 FastMCP），
  三个工具覆盖「只读自动 ALLOW / 工具名 sanitize / 非只读需 allow」三种形态。

本模块组装 hybrid toolkit：**function tool（L1/L2）+ MCP 工具共存**（§9.5 MCP Server 绑定）。
后端在 SIP「MCP Server 配置」页把 MCP server 绑定给 agent 后，``MCPClientManager``
持有对应 ``agentscope.mcp.MCPClient``；``AgentNodeRunner`` 每节点取出绑定 client 传给
本函数，最终 ``Toolkit(tools=[function tools], mcps=[...])`` 合并 —— 同一 agent 两类工具
都能被 LLM 调用。``build_toolkit`` 对「stateful 但未 connect」的 client 做防御性过滤
（AgentScope ``Toolkit.__init__`` 会对这种 client 抛 ValueError）。
"""
from __future__ import annotations

import logging

from agentscope.tool import FunctionTool, Toolkit

from .tools import build_l2_tools, build_local_tools, build_workspace_tools

log = logging.getLogger("agentflow.toolkit")


def _filter_connected_mcps(mcp_clients: list) -> list:
    """过滤出可注入 Toolkit 的 MCP client。

    stateful 但未 connect 的 client 会让 ``Toolkit.__init__`` 抛 ValueError
    （agentscope _toolkit.py）——防御性剔除；stateless（HTTP 一次性会话）无需连接可直接注入。
    """
    out = []
    for client in mcp_clients:
        try:
            disconnected = client.is_stateful and not client.is_connected
        except Exception:  # noqa: BLE001 —— 缺属性等防御，不因单 client 崩掉
            log.warning("MCP client 缺少连接状态属性，跳过注入 toolkit: %r", client)
            continue
        if disconnected:
            log.warning("MCP[%s] stateful 但未连接，跳过注入 toolkit", client.name)
            continue
        out.append(client)
    return out


def _build_function_tools(
    agent_name: str,
    *,
    sandbox_client=None,
    action_executor=None,
) -> list[FunctionTool]:
    """本地 function tool 包装：只读（CMDB/知识）+ 工作区 + L2 执行。

    **数据源查询不在此列**——日志/指标/K8s 已迁至 `aiops-datasource-mcp-server`
    （design-v5.5），经 ``mcp_clients`` 注入，不再有进程内直连实现。

    - 本地只读：``search_knowledge``（占位；CMDB 已随 ``locate_repo`` 迁至 MCP）
    - 工作区：读写本次 run 的代码工作区（§8.7）
    - L2：传 ``sandbox_client``→沙箱 run/write；传 ``action_executor``→§10.3 白名单动作
    """
    tools: list[FunctionTool] = []
    for t in build_local_tools(agent_name):
        tools.append(FunctionTool(
            func=t["func"], name=t["name"], description=t["description"],
            is_read_only=True,  # 本地只读工具
        ))
    for t in build_workspace_tools(agent_name):
        tools.append(FunctionTool(
            func=t["func"], name=t["name"], description=t["description"],
            is_read_only=t["read_only"],
        ))
    for t in build_l2_tools(agent_name, sandbox_client=sandbox_client, action_executor=action_executor):
        if t["func"] is None:
            continue
        tools.append(FunctionTool(
            func=t["func"], name=t["name"], description=t["description"],
            is_read_only=False,  # L2 是执行工具
        ))
    return tools


def build_toolkit(
    agent_name: str,
    *,
    mcp_clients: list | None = None,
    sandbox_client=None,
    action_executor=None,
) -> Toolkit:
    """为 agent 构建 hybrid Toolkit：本地 function tool 与绑定的 MCP 工具共存。

    本地部分 = 只读（知识）+ 工作区 + L2；**数据源查询与 CMDB 全部来自 MCP**
    （``mcp_clients``，由 agent_configs.mcp_server_ids 绑定决定）——design-v5.5。
    """
    func_tools = _build_function_tools(
        agent_name,
        sandbox_client=sandbox_client,
        action_executor=action_executor,
    )
    mcps = _filter_connected_mcps(mcp_clients or [])
    if mcps:
        return Toolkit(tools=func_tools, mcps=mcps)
    return Toolkit(tools=func_tools)
