# -*- coding: utf-8 -*-
"""审计日志记录（design §9.5 审计字段 + §8.8 审计表）。

每次工具调用写一条：tenant_id / tool_name / decision / run_id + node_id /
input（脱敏）/ ts。由 ToolPolicy 决策后调用，或由工具层统一包装。
"""
from __future__ import annotations

import re
from typing import Any

from ..statestore.base import StateStore

# 敏感字段脱敏（凭证/密钥类）
_SENSITIVE = re.compile(r"(?i)(password|token|secret|api[_-]?key|credential)")


def mask_input(obj: Any, max_len: int = 200) -> str:
    """输入脱敏：敏感字段值替换为 ***；截断超长。"""
    if isinstance(obj, dict):
        masked = {k: ("***" if _SENSITIVE.search(str(k)) else v) for k, v in obj.items()}
        text = str(masked)
    else:
        text = str(obj)
        # 形如 token=xxx / "token": "xxx" 的敏感值
        text = re.sub(r"(?i)((?:password|token|secret|api[_-]?key|credential)\s*[=:]\s*)[^,\s\"'}]+", r"\1***", text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


class AuditLogger:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def log_tool_call(
        self,
        *,
        tenant_id: str,
        tool_name: str,
        decision: str,
        agent: str,
        run_id: str,
        node_id: str,
        tool_input: Any = None,
    ) -> dict:
        """记录一次工具调用（§9.5 审计字段）。"""
        record = {
            "tenant_id": tenant_id, "tool_name": tool_name,
            "decision": decision, "agent": agent,
            "run_id": run_id, "node_id": node_id,
            "input_masked": mask_input(tool_input) if tool_input is not None else None,
        }
        await self._store.append_audit(tenant_id, tool_name=tool_name, decision=decision,
                                       run_id=run_id, node_id=node_id,
                                       input_masked=record["input_masked"], actor=agent)
        return record
