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
    stage: str = "other"  # 流水线阶段 detect/diagnose/fix/verify/deliver/learn（供 /agents 舰队分组）
    schema: dict = field(default_factory=dict)
    tools: list[ToolSpec] = field(default_factory=list)
    description: str = ""  # 一句话职责描述（供 API 控制面 /agents 列表展示）


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

# 一句话职责描述（依据 prompts.py SYSTEM_PROMPTS 提炼，供 API /agents 列表展示）
AGENT_DESCRIPTIONS: dict[str, str] = {
    "triage": "对 bug ticket 做症状分类（hang/crash/slow/degraded）",
    "log-analyst": "分析日志定位异常类型",
    "trace-analyst": "分析 trace 重建调用链，定位故障 span 与失败服务",
    "metrics-analyst": "分析 Prometheus 指标定位异常（CPU/内存/磁盘/延迟/错误率）",
    "infra-locator": "查询 K8s 资源状态，定位基础设施问题",
    "code-locator": "由服务名定位对应仓库与可疑代码",
    "knowledge-lookup": "在运维知识图谱中检索历史故障与处理方案",
    "root-cause": "综合多维证据给出根因（code_bug/infra_issue/config_issue/dependency_issue）",
    "fix-planner": "根据根因生成修复计划（止血/根治）",
    "fix-implementer": "在沙箱中实施代码修复并产出 diff",
    "infra-remediator": "通过 ActionExecutor 执行受限基础设施动作（scale/restart/patch）",
    "tester": "对修复运行测试与集成验证",
    "reviewer": "审查修复 diff，判断是否可提交",
    "committer": "把修复提交为 PR（幂等，external_operation_id=PR number）",
    "postmortem": "产出复盘报告",
}

# 流水线阶段（供 API 控制面 /agents 前端舰队分组展示：detect→diagnose→fix→verify→deliver→learn）
AGENT_STAGES: dict[str, str] = {
    "triage": "detect",
    "log-analyst": "detect",
    "trace-analyst": "detect",
    "metrics-analyst": "detect",
    "infra-locator": "detect",
    "code-locator": "detect",
    "knowledge-lookup": "detect",
    "root-cause": "diagnose",
    "fix-planner": "fix",
    "fix-implementer": "fix",
    "infra-remediator": "fix",
    "tester": "verify",
    "reviewer": "verify",
    "committer": "deliver",
    "postmortem": "learn",
}

AGENT_REGISTRY: dict[str, AgentSpec] = {
    name: AgentSpec(
        name=name,
        role="diagnose" if name in DIAGNOSE_AGENTS else "fix",
        stage=AGENT_STAGES.get(name, "other"),
        schema=AGENT_SCHEMAS.get(name, {}),
        tools=tools_for_agent(name),
        description=AGENT_DESCRIPTIONS.get(name, ""),
    )
    for name in DIAGNOSE_AGENTS + FIX_AGENTS
}


def get_agent_spec(name: str) -> AgentSpec:
    if name not in AGENT_REGISTRY:
        raise KeyError(f"未知 agent: {name!r}（注册表见 agentflow/agents/registry.py）")
    return AGENT_REGISTRY[name]


def all_agents() -> list[str]:
    return DIAGNOSE_AGENTS + FIX_AGENTS
