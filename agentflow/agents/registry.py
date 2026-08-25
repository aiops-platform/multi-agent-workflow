# -*- coding: utf-8 -*-
"""15-agent 编队注册表（design §7）。

映射 agent 名 → 职责 / 工具可见性 / 输出 Schema。executor 的 node_runner
通过 ``AGENT_REGISTRY`` 把节点调度到对应职能智能体。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .prompts import AGENT_SCHEMAS
from .tools import ToolSpec, tools_for_agent


@dataclass
class AgentSpec:
    name: str
    role: str  # diagnose(只读) | fix(修复)
    schema: dict = field(default_factory=dict)
    tools: list[ToolSpec] = field(default_factory=list)


# 诊断侧（只读，全部 L1）
DIAGNOSE_AGENTS = [
    "triage",
    "log-analyst",
    "trace-analyst",
    "metrics-analyst",
    "infra-locator",
    "code-locator",
    "knowledge-lookup",
    "root-cause",
]
# 解决侧（L1 + L2）
FIX_AGENTS = [
    "fix-planner",
    "fix-implementer",
    "infra-remediator",
    "tester",
    "reviewer",
    "committer",
    "postmortem",
]

AGENT_REGISTRY: dict[str, AgentSpec] = {
    name: AgentSpec(
        name=name,
        role="diagnose" if name in DIAGNOSE_AGENTS else "fix",
        schema=AGENT_SCHEMAS.get(name, {}),
        tools=tools_for_agent(name),
    )
    for name in DIAGNOSE_AGENTS + FIX_AGENTS
}


def get_agent_spec(name: str) -> AgentSpec:
    if name not in AGENT_REGISTRY:
        raise KeyError(f"未知 agent: {name!r}（注册表见 agentflow/agents/registry.py）")
    return AGENT_REGISTRY[name]


def all_agents() -> list[str]:
    return DIAGNOSE_AGENTS + FIX_AGENTS
