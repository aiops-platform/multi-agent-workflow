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

from .tools import build_l1_tools


def build_toolkit(
    agent_name: str,
    *,
    use_mock: bool = True,
    mcp_clients: list | None = None,
    cmdb=None,
    datasource=None,
) -> Toolkit:
    """为 agent 构建 Toolkit：L1 只读工具（mock / 真实数据源 / MCP client）。

    - ``use_mock=True``：L1 工具用 ``FunctionTool`` 包装 mock 实现（本地联调），
      不依赖任何数据源凭证；传 ``cmdb`` 时 ``locate_code`` 走 CMDB（§9.4）；
      传 ``datasource`` 时日志/指标/基础设施工具绑定真实 testbed。
    - 数据源就绪：传 ``mcp_clients``（每个数据源一个 client），并置 use_mock=False。
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
        return Toolkit(tools=tools)
    # 真实数据源路径：全部经 MCP client
    return Toolkit(tools=[], mcps=list(mcp_clients or []))
