"""Mock MCP server（mcp SDK 1.x FastMCP）。

⚠️ AgentScope 2.0.3 的 MCP 能力按 mcp v1 API 编写，本仓库已显式 pin ``mcp>=1.13,<2.0``，
故 mock server 用 ``mcp.server.fastmcp.FastMCP``（见 docs/MCP_SERVER_CONFIG_zh-CN.md）。

三个工具覆盖 MCP 工具的三种权限形态，供 MCP 配置页「测试连接」联调 + pytest：
- ``get_weather``   —— ``readOnlyHint=True`` → AgentScope 只读路径自动 ALLOW；
- ``query.repo``    —— 工具名含 ``.`` → 验证 sanitize（LLM 名 ``mcp__mock-mcp__queryXrepo``）；
- ``send_alert``    —— 无只读标注 → 非只读 MCP 工具，DONT_ASK 下必须有 allow 规则才能执行。

用法：
    # stdio（默认）：pytest / AgentNodeRunner 经 StdioMCPConfig 拉起子进程
    python scripts/mock_mcp_server.py
    # streamable HTTP（前端「测试连接」http transport 用）
    python scripts/mock_mcp_server.py --http --port 8901
"""
from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(name="mock-mcp")


@mcp.tool(
    name="get_weather",
    description="查询城市天气（只读）。参数 city：城市名",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def get_weather(city: str) -> str:
    return f"city={city} weather=sunny temp=26C"


@mcp.tool(
    name="query.repo",
    description="查询仓库信息（只读；工具名含 `.` 以验证 AgentScope sanitize）。参数 repo：仓库路径",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def query_repo(repo: str) -> str:
    return f"repo={repo} branch=main"


@mcp.tool(
    name="send_alert",
    description="发送一条告警（执行型工具：无只读标注，需 allow 规则）。参数 title：告警标题；severity：级别",
)
async def send_alert(title: str, severity: str = "warning") -> str:
    return f"alert sent title={title} severity={severity}"


def main() -> None:
    parser = argparse.ArgumentParser(description="agentflow mock MCP server")
    parser.add_argument("--http", action="store_true", help="以 streamable HTTP 运行（默认 stdio）")
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args()
    if args.http:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
