# -*- coding: utf-8 -*-
"""M5：审批超时 Sweeper（§8.9）+ 审计日志（§9.5/§8.8）+ 通知。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentflow.approval.notifier import ApprovalNotifier
from agentflow.approval.sweeper import ApprovalSweeper
from agentflow.audit.logger import AuditLogger, mask_input
from agentflow.queue.memory import InMemoryQueue
from agentflow.statestore.base import APPROVAL_TIMED_OUT, APPROVAL_WAITING
from agentflow.statestore.memory import InMemoryStateStore
from agentflow.statestore.sqlite import SqliteStateStore


# ======================================================================
# 审计日志
# ======================================================================
def test_mask_input_sensitive() -> None:
    assert "token" in mask_input({"token": "sk-abc", "q": "logs"})
    assert "sk-abc" not in mask_input({"token": "sk-abc"})  # 值已脱敏
    assert mask_input("x" * 500).endswith("...")


async def test_audit_logger_memory() -> None:
    store = InMemoryStateStore()
    logger = AuditLogger(store)
    await logger.log_tool_call(tenant_id="team-alpha", tool_name="query_logs",
                               decision="ALLOW", agent="log-analyst",
                               run_id="run_1", node_id="logs", tool_input={"service": "order-service"})
    logs = await store.get_audit_logs(tenant_id="team-alpha")
    assert len(logs) == 1
    assert logs[0]["tool_name"] == "query_logs"
    assert logs[0]["decision"] == "ALLOW"
    assert logs[0]["tenant_id"] == "team-alpha"


async def test_audit_logger_sqlite() -> None:
    store = SqliteStateStore(":memory:")
    await store.connect()
    logger = AuditLogger(store)
    await logger.log_tool_call(tenant_id="t1", tool_name="sandbox_run_python",
                               decision="DENY", agent="tester", run_id="r1", node_id="test")
    logs = await store.get_audit_logs(run_id="r1")
    assert len(logs) == 1 and logs[0]["decision"] == "DENY"
    await store.close()


# ======================================================================
# 审批超时 Sweeper（§8.9）
# ======================================================================
async def _expired_approval(store, *, timeout_secs: float = -10, run_id: str = "run_1") -> str:
    timeout_at = (datetime.now(timezone.utc) + timedelta(seconds=timeout_secs)).isoformat()
    return await store.create_approval(
        run_id, "approve-changes", "team-alpha",
        params={"name": "审批修复方案"}, approvers=["lead"], timeout_at=timeout_at,
    )


async def test_sweeper_timeout_cas_and_resume() -> None:
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    sweeper = ApprovalSweeper(store, queue, interval=1)

    await store.create_run("run_1", "team-alpha", "snap", {})
    await store.put_node("run_1", "team-alpha", "approve-changes", {"status": "waiting_approval"})
    await _expired_approval(store)

    timed_out = await sweeper.run_once()
    assert len(timed_out) == 1

    # 审批终态 TIMED_OUT（CAS 生效）
    ap = await store.get_approval("run_1", "approve-changes")
    assert ap["status"] == APPROVAL_TIMED_OUT

    # 节点 rejected-canceled
    nodes = await store.get_nodes("run_1")
    assert nodes["approve-changes"]["status"] == "rejected-canceled"

    # resume 发布到 run.command
    msgs = []
    async for m in queue.subscribe("run.command"):
        msgs.append(m)
        if len(msgs) >= 1:
            break
    assert msgs[0]["type"] == "resume"
    assert msgs[0]["trigger"] == "approval_timeout"


async def test_sweeper_skips_future_approval() -> None:
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    sweeper = ApprovalSweeper(store, queue, interval=1)
    await _expired_approval(store, timeout_secs=3600, run_id="run_f")
    assert await sweeper.run_once() == []


async def test_sweeper_cas_no_double_timeout() -> None:
    """终态不可逆：已 TIMED_OUT 的审批不被重复处理。"""
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    sweeper = ApprovalSweeper(store, queue, interval=1)
    await store.create_run("run_2", "team-alpha", "snap", {})
    await store.put_node("run_2", "team-alpha", "approve", {"status": "waiting_approval"})
    await _expired_approval(store, run_id="run_2")

    assert len(await sweeper.run_once()) == 1
    assert await sweeper.run_once() == []  # 第二轮无新增
    ap = await store.get_approval("run_2", "approve-changes")
    assert ap["status"] == APPROVAL_TIMED_OUT


# ======================================================================
# 通知
# ======================================================================
async def test_tool_policy_audit_flow() -> None:
    """§9.5：ToolPolicy 决策 → AuditLogger 写审计（deny 也记录）。"""
    from agentflow.audit.logger import AuditLogger
    from agentflow.sandbox.policy import ToolPolicy

    store = InMemoryStateStore()
    policy = ToolPolicy()
    logger = AuditLogger(store)

    for tool, agent, expected in [
        ("query_logs", "log-analyst", "ALLOW"),
        ("scale_deployment", "infra-remediator", "DENY"),  # team-alpha deny
        ("unknown_tool", "triage", "DENY"),
    ]:
        decision = policy.decide(tool_name=tool, agent=agent, tenant_id="team-alpha")
        assert decision == expected
        await logger.log_tool_call(tenant_id="team-alpha", tool_name=tool, decision=decision,
                                   agent=agent, run_id="run_a", node_id="n", tool_input={"k": tool})

    logs = await store.get_audit_logs(tenant_id="team-alpha")
    assert len(logs) == 3
    assert {l["decision"] for l in logs} == {"ALLOW", "DENY"}


# ======================================================================
# 通知
# ======================================================================
async def test_notifier_record() -> None:
    n = ApprovalNotifier()
    r = await n.notify(kind="waiting", run_id="run_1", node_id="approve",
                       tenant_id="team-alpha", approvers=["lead"])
    assert r["kind"] == "waiting"
    assert r["approvers"] == ["lead"]
