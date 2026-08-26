# -*- coding: utf-8 -*-
"""审批通知（design §5/§8.3 审批流）。

本地 MVP：日志通知（approvers 队列、通知渠道为占位接口，M6 接邮件/Slack/webhook）。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("agentflow.approval.notifier")

_KIND_TEXT = {
    "waiting": "待审批",
    "approved": "已通过",
    "rejected": "已拒绝",
    "timeout": "已超时（自动拒绝）",
}


class ApprovalNotifier:
    async def notify(
        self,
        *,
        kind: str,
        run_id: str,
        node_id: str,
        tenant_id: str,
        approvers: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict:
        """通知审批方。返回通知记录（本地 log 实现）。"""
        approvers = approvers or []
        text = (
            f"[{_KIND_TEXT.get(kind, kind)}] run={run_id} node={node_id} "
            f"tenant={tenant_id} approvers={approvers}"
        )
        log.info("审批通知: %s", text)
        return {"kind": kind, "run_id": run_id, "node_id": node_id,
                "tenant_id": tenant_id, "approvers": approvers, "text": text}
