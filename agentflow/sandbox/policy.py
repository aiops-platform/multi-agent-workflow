# -*- coding: utf-8 -*-
"""ToolPolicy：租户级工具权限 + 资源限制（design §9.5 / §10.2）。

- 决策：deny 优先 → allow → 兜底 DENY（§9.5）
- agent 可用工具 = 该 agent 注册工具 ∩ 租户 allow − 租户 deny
- 与 AgentScope 权限层配合：本模块产出规则 → ``scopes.build_permission_context``
  转成 PermissionContext；本模块同时用于审计与 L2 资源限制。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..agents.tools import TOOL_REGISTRY, ToolSpec

log = logging.getLogger("agentflow.sandbox.policy")


@dataclass
class TenantToolConfig:
    tenant_id: str
    allow: list[str] = field(default_factory=list)  # 空 = 继承 agent 注册工具
    deny: list[str] = field(default_factory=list)
    # §10.2 每租户资源限制（可覆盖 Tool Registry 默认）
    resource_limits: dict[str, dict] = field(default_factory=dict)


class ToolPolicy:
    """租户级工具策略。决策结果可写入审计表（§8.8 审计字段）。"""

    def __init__(self, tenants: dict[str, TenantToolConfig] | None = None) -> None:
        self._tenants: dict[str, TenantToolConfig] = tenants or {
            # 默认：team-alpha 只允许诊断/沙箱只读，禁止高风险动作（写权限走审批）
            "team-alpha": TenantToolConfig(
                tenant_id="team-alpha",
                allow=["query_logs", "get_trace", "query_metrics", "check_infra", "describe_pod",
                       "locate_code", "search_knowledge", "sandbox_run_python", "sandbox_run_shell",
                       "sandbox_write_file"],
                deny=["scale_deployment", "restart_pod", "patch_resources"],  # §10.3 写动作需审批
            ),
        }

    def get_spec(self, tool_name: str) -> ToolSpec | None:
        return TOOL_REGISTRY.get(tool_name)

    def decide(self, *, tool_name: str, agent: str, tenant_id: str) -> str:
        """返回 ALLOW / DENY（deny 优先 → allow → 兜底 DENY，§9.5）。"""
        cfg = self._tenants.get(tenant_id)
        if cfg and tool_name in cfg.deny:
            return "DENY"
        spec = self.get_spec(tool_name)
        if spec is None:
            return "DENY"  # 未注册工具兜底拒绝
        if agent not in spec.agents:
            return "DENY"  # 该 agent 未注册此工具
        if cfg and cfg.allow and tool_name not in cfg.allow:
            return "DENY"
        return "ALLOW"

    def allowed_tools(self, agent: str, tenant_id: str) -> list[str]:
        cfg = self._tenants.get(tenant_id)
        agent_tools = [t for t, s in TOOL_REGISTRY.items() if agent in s.agents]
        allow = cfg.allow if (cfg and cfg.allow) else agent_tools
        deny = set(cfg.deny) if cfg else set()
        return [t for t in agent_tools if t in allow and t not in deny]

    def resource_limit(self, tool_name: str, tenant_id: str) -> dict:
        """L2 工具的资源限制（§10.2 / Tool Registry），租户可覆盖。"""
        cfg = self._tenants.get(tenant_id)
        overrides = (cfg.resource_limits.get(tool_name) or {}) if cfg else {}
        spec = self.get_spec(tool_name)
        base = {
            "timeout": spec.timeout if spec else 300,
            "result_limit": spec.result_limit if spec else 1_048_576,
        }
        return {**base, **overrides}

    def audit(self, *, tool_name: str, agent: str, tenant_id: str, decision: str, run_id: str, node_id: str) -> dict:
        """§9.5 审计记录字段。"""
        return {
            "tenant_id": tenant_id, "tool_name": tool_name, "agent": agent,
            "decision": decision, "run_id": run_id, "node_id": node_id,
        }
