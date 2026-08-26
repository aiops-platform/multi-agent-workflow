# -*- coding: utf-8 -*-
"""MCP 数据源接入（design §5 AgentScope 原生支持 / §7 数据源 MCP）。

集成模式已在 spike 验证：
- ``spike/s-002-agent-scope-mcp/mcp_adapter_prototype.py``：真实模型自动发现并调用
  MCP 工具（Toolkit(tools=[], mcps=[client])，见 S-002 报告）。
- ``spike/s-011-agent-smoke/mock_mcp_server.py``：mock MCP server（query_logs /
  get_trace / query_metrics / check_infra / locate_code / search_knowledge）。

本模块是 M1 的接入骨架：数据源（日志平台 / 链路 / CMDB / 知识图谱）就绪后，
在此把每个数据源包装为 MCP client 注入 Toolkit。当前提供本地 mock toolkit。
"""
from __future__ import annotations

from agentscope.tool import FunctionTool, Toolkit

from .tools import build_l1_tools, build_l2_tools


def build_toolkit(
    agent_name: str,
    *,
    use_mock: bool = True,
    mcp_clients: list | None = None,
    cmdb=None,
    datasource=None,
    sandbox_client=None,
    action_executor=None,
) -> Toolkit:
    """为 agent 构建 Toolkit：L1 只读工具 + L2 执行工具（§4.1 推理/执行分离）。

    - L1（只读，mock/真实数据源/MCP）：传 ``cmdb``→CMDB，传 ``datasource``→真实 testbed。
    - L2（执行）：传 ``sandbox_client``（SandboxClient）→ 沙箱 run/write 工具；
      传 ``action_executor``（ActionExecutor）→ §10.3 白名单动作工具。
      未传执行器时 L2 工具不可用（本地不接沙箱时）。
    """
    if use_mock:
        tools = [
            FunctionTool(
                func=t["func"],
                name=t["name"],
                description=t["description"],
                is_read_only=True,
            )
            for t in build_l1_tools(agent_name, use_mock=True, cmdb=cmdb, datasource=datasource)
            if t["func"] is not None
        ]
        for t in build_l2_tools(agent_name, sandbox_client=sandbox_client, action_executor=action_executor):
            if t["func"] is None:
                continue
            tools.append(FunctionTool(
                func=t["func"], name=t["name"], description=t["description"],
                is_read_only=False,  # L2 是执行工具，非只读
            ))
        return Toolkit(tools=tools)
    # 真实数据源路径：全部经 MCP client
    return Toolkit(tools=[], mcps=list(mcp_clients or []))
