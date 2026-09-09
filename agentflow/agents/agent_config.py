# -*- coding: utf-8 -*-
"""AgentSpec 运行时解析器：把 DB 配置（agent_configs）与静态内置默认合并成最终生效值。

纯 agents 层，**不依赖 api 包**（避免 agents→api 反向依赖）。构造时喂入 ``AgentConfigStore.list()``
的行（启动或每次 CRUD 后重建），据此解析：
- ``resolve(name)``：DB 行存在 → 用行的覆盖值（NULL 字段回退静态内置）；行不存在但属内置 15 → 纯静态默认；
  两者都不是 → None（运行时走 scopes 静态回退）。
- ``server_ids_for(name)``：MCP 绑定两态（空 set→无 server；非空 set→精确子集）——v1.12.1 起
  去掉「未配置=全量 enabled」默认，改「没配置就没有 server」。供 ``MCPClientManager.clients_for_agent`` 过滤。
- ``all()``：内置 15（DB 覆盖或静态）∪ 自定义行（origin='custom'），供 ``GET /agents`` 合并视图。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .prompts import AGENT_SCHEMAS, SYSTEM_PROMPTS
from .registry import (
    AGENT_DESCRIPTIONS,
    AGENT_STAGES,
    DIAGNOSE_AGENTS,
    FIX_AGENTS,
)

_DEFAULT_PROMPT = "你是 AI 运维平台智能体。"

# 内置 15 的顺序基准（诊断侧在前，与既有 AGENT_REGISTRY / GET /agents 顺序一致）
_BUILTIN_ORDER = DIAGNOSE_AGENTS + FIX_AGENTS
_BUILTIN_SET = set(_BUILTIN_ORDER)


@dataclass
class ResolvedAgent:
    """合并后的最终生效 AgentSpec（供运行时装配 / GET /agents 展示）。"""

    name: str
    role: str  # diagnose | fix
    stage: str
    description: str
    system_prompt: str
    schema: dict = field(default_factory=dict)
    enabled: bool = True
    origin: str = "builtin"  # builtin | custom
    mcp_server_ids: set[str] = field(default_factory=set)  # 空=无 server；非空=精确子集
    reasoning_enabled: bool = False  # Agent 级启用推理（thinking-enabled DeepSeek 模型 + CoT 落 trace）


def _static_resolved(name: str) -> ResolvedAgent | None:
    """内置名 → 纯静态默认 ResolvedAgent（无 DB 覆盖时用）。非内置返回 None。"""
    if name not in _BUILTIN_SET:
        return None
    return ResolvedAgent(
        name=name,
        role="diagnose" if name in DIAGNOSE_AGENTS else "fix",
        stage=AGENT_STAGES.get(name, "other"),
        description=AGENT_DESCRIPTIONS.get(name, ""),
        system_prompt=SYSTEM_PROMPTS.get(name, _DEFAULT_PROMPT),
        schema=AGENT_SCHEMAS.get(name, {}),
        enabled=True,
        origin="builtin",
    )


def _resolve_row(row: dict) -> ResolvedAgent:
    """DB 行 → 合并静态回退后的 ResolvedAgent。NULL 可空列 → 回退内置；行字段优先。"""
    name = row["name"]
    return ResolvedAgent(
        name=name,
        role=row["role"],
        stage=row.get("stage", "other"),
        description=row.get("description") or AGENT_DESCRIPTIONS.get(name, ""),
        system_prompt=row.get("system_prompt") or SYSTEM_PROMPTS.get(name, _DEFAULT_PROMPT),
        schema=row.get("schema") if row.get("schema") is not None else AGENT_SCHEMAS.get(name, {}),
        enabled=bool(row.get("enabled", True)),
        origin=row.get("origin", "custom"),
        mcp_server_ids=set(row["mcp_server_ids"]) if row.get("mcp_server_ids") else set(),
        reasoning_enabled=bool(row.get("reasoning_enabled", False)),
    )


class AgentConfigResolver:
    """配置行索引 + 合并解析。每次 DB 变更（init seed / CRUD）后重建一个实例。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows: dict[str, dict] = {r["name"]: r for r in rows}

    def resolve(self, name: str) -> ResolvedAgent | None:
        """最终生效 AgentSpec：DB 行（含覆盖合并）> 内置静态默认 > None。"""
        row = self._rows.get(name)
        if row is not None:
            return _resolve_row(row)
        return _static_resolved(name)

    def server_ids_for(self, name: str) -> set[str]:
        """该 agent 可调用的 MCP server id 集合；空 set = 无 server（未配置/明确不绑）。

        两态语义（v1.12.1）：``mcp_server_ids`` 为 NULL 或空数组 → 空 set（没有 MCP server）；
        非空数组 → 该精确子集。resolver 未命中（非内置也非 DB 行）同样返回空 set。
        """
        resolved = self.resolve(name)
        return resolved.mcp_server_ids if resolved is not None else set()

    def all(self) -> list[ResolvedAgent]:
        """内置 15（DB 覆盖或静态）→ 自定义行；供 GET /agents 合并视图 / 舰队分组。"""
        out: list[ResolvedAgent] = []
        seen: set[str] = set()
        for name in _BUILTIN_ORDER:
            out.append(self.resolve(name))
            seen.add(name)
        for name, row in self._rows.items():
            if name in seen:
                continue
            out.append(_resolve_row(row))
            seen.add(name)
        return out

    def names(self) -> list[str]:
        return [a.name for a in self.all()]

    def get(self, name: str) -> dict | None:
        """原始配置行（未合并），供详情/编辑回填判断「是否已覆盖」。"""
        return self._rows.get(name)
