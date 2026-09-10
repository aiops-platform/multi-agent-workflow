"""执行上下文（design-v5.3 §7）：DAG 节点执行期的租户上下文。

``current_tenant`` 由 ``DAGExecutor._exec_node`` 在节点执行前置位（executor 持有
tenant_id），供 node_runner（AgentNodeRunner）读取——per-tenant MCP store、
per-tenant agent 配置解析都在 runner 侧按它路由。ContextVar 隔离并行波次
（asyncio task 各自的上下文）。
"""
from __future__ import annotations

from contextvars import ContextVar

current_tenant: ContextVar[str | None] = ContextVar("agentflow_current_tenant", default=None)

# ``current_run`` 同理由 executor 在节点执行前置位，供 node_runner 解析「本次 run 的
# 工作区」（§8.7.2 布局 workspace/{tenant}/{run}/repos/{service}）——修复侧 agent
# （fix-implementer/tester/reviewer/committer）的工作区工具按它定位。
current_run: ContextVar[str | None] = ContextVar("agentflow_current_run", default=None)
