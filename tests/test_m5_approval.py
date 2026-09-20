"""M5：审批超时 Sweeper（§8.9）+ 审计日志（§9.5/§8.8）+ 通知。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentflow.approval.notifier import ApprovalNotifier
from agentflow.approval.sweeper import ApprovalSweeper
from agentflow.audit.logger import AuditLogger, mask_input
from agentflow.core.dag import DAG, DONE, REJECTED_CANCELED
from agentflow.executor.dag_executor import DAGExecutor
from agentflow.queue.base import topic_command
from agentflow.queue.memory import InMemoryQueue
from agentflow.statestore.base import (
    APPROVAL_APPROVED,
    APPROVAL_TIMED_OUT,
    APPROVAL_WAITING,
)
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
    timeout_at = (datetime.now(UTC) + timedelta(seconds=timeout_secs)).isoformat()
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
    async for m in queue.subscribe(topic_command("team-alpha")):
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


async def test_sweeper_accepts_datetime_timeout_at_pg_adapter(monkeypatch) -> None:
    """回归：PG 适配器把 TIMESTAMPTZ 列读回 datetime（非 str），sweeper 不得再抛
    ``TypeError: fromisoformat: argument must be str``（曾导致超时审批永远无法处理）。"""
    store = InMemoryStateStore()
    queue = InMemoryQueue()
    sweeper = ApprovalSweeper(store, queue, interval=1)
    await store.create_run("run_pg", "team-alpha", "snap", {})
    await store.put_node("run_pg", "team-alpha", "approve-changes", {"status": "waiting_approval"})
    await _expired_approval(store, run_id="run_pg")  # 真实 ISO str 落库，aid=ap_run_pg_approve-changes

    real_get_pending = store.get_pending_approvals

    async def _pg_pending():
        rows = await real_get_pending()
        for r in rows:
            r["timeout_at"] = datetime.fromisoformat(r["timeout_at"])  # 模拟 PG 读回 datetime
        return rows

    monkeypatch.setattr(store, "get_pending_approvals", _pg_pending)

    timed_out = await sweeper.run_once()
    assert len(timed_out) == 1
    ap = await store.get_approval("run_pg", "approve-changes")
    assert ap["status"] == APPROVAL_TIMED_OUT


# ======================================================================
# 审批超时 → Resume 收敛（§8.9 回归，必须在 SQLite 下验证）
# ======================================================================
async def test_approval_timeout_resume_converges_sqlite() -> None:
    """回归：审批超时后 Resume 必须沿拒绝路径收敛到 done，而非永卡 waiting_approval。

    曾有 bug：sweeper 写 rejected-canceled 走 update_node_status，只更新 status/output
    列不写 cp；而 from_checkpoint 只读 cp 且白名单不含 rejected-canceled → 超时后
    resume 把节点当 waiting_approval 重新挂起（approvals 已 TIMED_OUT，CAS 永远失败）。
    InMemory 测试掩盖了它（status 与 cp 是同一个 dict，天然"同步"）。
    """
    store = SqliteStateStore(":memory:")
    await store.connect()
    dag = DAG.build(
        {
            "plan": {"agent": "fix-planner"},
            "approve-changes": {
                "kind": "approval", "name": "审批修复方案",
                "approvers": ["lead"], "timeout": -1,  # 负超时 → 创建即过期
                # 必须显式 continue：默认 abort 下，下面那条拒绝边永远不可达
                # （本测试要验的正是"超时后沿拒绝路径收敛"）。
                "on_reject": "continue",
            },
            "recap": {"agent": "postmortem"},
        },
        [
            {"from": "plan", "to": "approve-changes"},
            {"from": "approve-changes", "to": "recap",
             "when": "$.nodes.approve-changes.output.approved == false"},
        ],
    )
    calls: list[str] = []

    async def runner(node, params):
        calls.append(node.id)
        return {"ok": True}

    ex = DAGExecutor("run_t", "team-alpha", dag, store, node_runner=runner)
    assert await ex.run() == "waiting_approval"

    sweeper = ApprovalSweeper(store, InMemoryQueue(), interval=1)
    assert len(await sweeper.run_once()) == 1

    # cp 与 status 列同步（曾是 bug 根因：两处真相分叉）
    cps = await store.get_nodes("run_t")
    assert cps["approve-changes"]["status"] == REJECTED_CANCELED
    assert cps["approve-changes"]["output"] == {"approved": False, "reason": "timeout"}

    ex2 = await DAGExecutor.from_checkpoint(
        "run_t", "team-alpha", dag, store, node_runner=runner
    )
    # rejected-canceled 是终态：保留，不重置 pending、不重新挂起审批
    assert ex2.node_states["approve-changes"]["status"] == REJECTED_CANCELED
    assert await ex2.run() == "done"
    assert ex2.node_states["recap"]["status"] == DONE  # 沿拒绝路径收敛
    await store.close()


# ======================================================================
# 审批 CAS 时间原子判定（§8.3.2 AND timeout_at > NOW()）
# ======================================================================
@pytest.mark.parametrize(
    "make_store",
    [lambda: InMemoryStateStore(), lambda: SqliteStateStore(":memory:")],
    ids=["memory", "sqlite"],
)
async def test_cas_time_guard(make_store) -> None:
    """approve/reject 仅在超时窗口内可批；TIMED_OUT 仅超时后可置。

    曾有竞态：CAS 只有 status 谓词，「已超时但 sweeper 尚未扫到」窗口内 approve
    仍能成功，审批在超时后依旧被放行。
    """
    store = make_store()
    if isinstance(store, SqliteStateStore):
        await store.connect()
    try:
        await _expired_approval(store, run_id="r_exp")  # 已过期（-10s）
        # 已过期 → 不可批
        assert not await store.cas_update_approval(
            "ap_r_exp_approve-changes", APPROVAL_WAITING, APPROVAL_APPROVED, by="u"
        )
        # 已过期 → sweeper 可置 TIMED_OUT
        assert await store.cas_update_approval(
            "ap_r_exp_approve-changes", APPROVAL_WAITING, APPROVAL_TIMED_OUT
        )

        await _expired_approval(store, timeout_secs=3600, run_id="r_ok")  # 未过期
        # 未过期 → 可批
        assert await store.cas_update_approval(
            "ap_r_ok_approve-changes", APPROVAL_WAITING, APPROVAL_APPROVED, by="u"
        )

        await _expired_approval(store, timeout_secs=3600, run_id="r_no")  # 未过期
        # 未过期 → 不可置 TIMED_OUT
        assert not await store.cas_update_approval(
            "ap_r_no_approve-changes", APPROVAL_WAITING, APPROVAL_TIMED_OUT
        )
    finally:
        if isinstance(store, SqliteStateStore):
            await store.close()


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
        # 本地只读工具（数据源/CMDB 工具已迁 MCP，不在本地注册表 → 会被判 DENY）
        ("search_knowledge", "knowledge-lookup", "ALLOW"),
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


# ----------------------------------------------------------------------
# sweeper 的逐租户隔离
# ----------------------------------------------------------------------
async def test_sweeper_one_bad_tenant_does_not_starve_the_rest(monkeypatch) -> None:
    """一个租户坏掉，**排在它后面的租户仍要被扫到**。

    回归背景（实测踩过）：`run_once` 逐租户 `await self._store(tid)` 且**没有
    try/except** → 某个租户的 `db_ref_enc` 解不开时抛异常、整个 for 中断，
    排在它后面的租户全部扫不到。而且**坏租户的位置决定谁受害**——排在最后时
    "恰好没人受影响"，看起来一切正常。没有任何报错指向真正的原因。
    """
    store = InMemoryStateStore()
    good_a, good_c = SqliteStateStore(":memory:"), SqliteStateStore(":memory:")
    for s in (good_a, good_c):
        await s.connect()

    order: list[str] = []

    async def resolve(tid):
        order.append(tid)
        if tid == "bad":
            raise ValueError("db_ref 解密失败（密钥不匹配？）")
        return {"a": good_a, "c": good_c}[tid]

    class _Router:
        async def resolve(self, tid):
            return await resolve(tid)

    sweeper = ApprovalSweeper(
        _Router(), InMemoryQueue(), interval=1,
        tenants_provider=lambda: _order(),
    )

    async def _order():
        return ["a", "bad", "c"]

    timed = await sweeper.run_once()

    # 三个都被尝试过（坏的那个没让 for 提前退出）
    assert order == ["a", "bad", "c"], f"坏租户中断了整轮：{order}"
    assert timed == []
