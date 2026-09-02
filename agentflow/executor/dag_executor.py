"""并发 DAG 执行器（design §8.2 join/skip 语义 + §8.6 Worker 生命周期）。

语义要点（对应 S-008b / S-010b 实测）：
1. **并行分支并发执行**：同一波 ready 的 agent 节点用 ``asyncio.gather`` 并发跑；
   某节点完成立即唤醒下游 approval，不等待同波慢分支。
2. **join 策略**（§8.2.1）：``any`` 至少一条 ACTIVE 入边即 READY；
   ``all`` 全部 required_edges ACTIVE 才 READY；所有入边 INACTIVE → SKIPPED 级联。
3. **审批节点参与 skip**（S-010b）：approval 的 when 不满足 → SKIPPED 而非 WAITING。
4. **审批挂起**：approval 节点一旦满足条件即置 WAITING_APPROVAL 并落盘；
   ``run()`` 仅在 ready 集为空且存在 waiting_approval 时返回 → Worker 释放（§8.6）。
5. **节点级 checkpoint**：每节点完成后持久化 → 任意 crash 可 Resume（S-010b）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.dag import (
    DAG,
    DONE,
    PENDING,
    REJECTED,
    RUNNING,
    SKIPPED,
    TERMINAL,
    WAITING_APPROVAL,
    Node,
)
from ..core.expressions import eval_condition
from ..statestore.base import (
    APPROVAL_APPROVED,
    APPROVAL_REJECTED,
    APPROVAL_WAITING,
    StateStore,
)
from .idempotency import execute_with_idempotency

log = logging.getLogger("agentflow.executor")

# 节点 runner：接收 (node, resolved_params) 返回输出
NodeRunner = Callable[[Node, dict], Awaitable[Any]]


class WorkflowNodeFailed(Exception):
    def __init__(self, node_id: str, cause: Exception) -> None:
        super().__init__(f"节点 {node_id} 执行失败（重试耗尽）: {cause}")
        self.node_id = node_id
        self.cause = cause


class WorkflowStalledError(Exception):
    """ready 集为空、无 waiting_approval、又未全部终态 —— DAG 死锁或逻辑错误。"""


class ApprovalRaceError(Exception):
    """审批 CAS 冲突（已被并发操作推进到终态）。"""


def _resolve_param(value: Any, ctx: dict) -> Any:
    """解析 `$.nodes.X.output[.field]` / `$.inputs.X` 引用；普通值原样返回。"""
    if isinstance(value, str) and value.startswith("$."):
        path = value[2:]  # 去掉 "$."
        if path.startswith("nodes."):
            rest = path[len("nodes."):]
            parts = rest.split(".")
            node_id = parts[0]
            field_path = ".".join(parts[1:])
            node_output = ctx["nodes"].get(node_id, {}).get("output")
            if node_output is None:
                return None
            # "output" 是标准访问器（取节点输出值），不是节点输出的字段
            if field_path == "output":
                return node_output
            field_path = field_path.removeprefix("output.")
            if not field_path:
                return node_output
            cur: Any = node_output
            for p in field_path.split("."):
                if p == "":
                    continue
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return None
            return cur
        if path.startswith("inputs."):
            cur = ctx.get("inputs", {})
            for p in path[len("inputs."):].split("."):
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return None
            return cur
    if isinstance(value, dict):
        return {k: _resolve_param(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_param(v, ctx) for v in value]
    return value


def resolve_params(params: dict, ctx: dict) -> dict:
    return {k: _resolve_param(v, ctx) for k, v in params.items()}


class DAGExecutor:
    """不可变 DAG 的并发执行器。同一 executor 实例可先 run() 到 waiting_approval、
    再 approve() 后继续 run()（审批 Resume 的进程内形态）。"""

    def __init__(
        self,
        run_id: str,
        tenant_id: str,
        dag: DAG,
        store: StateStore,
        node_runner: NodeRunner | None = None,
        inputs: dict | None = None,
    ) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.dag = dag
        self.store = store
        self.inputs = inputs or {}
        self.node_runner = node_runner or self._default_runner
        self.node_states: dict[str, dict] = {
            nid: {"status": PENDING, "output": None} for nid in dag.nodes
        }
        self.failed: list[str] = []

    # ==================================================================
    # 查询
    # ==================================================================
    def get_status(self, nid: str) -> str:
        return self.node_states[nid]["status"]

    def get_output(self, nid: str) -> Any:
        return self.node_states[nid].get("output")

    def has_waiting_approval(self) -> bool:
        return any(st["status"] == WAITING_APPROVAL for st in self.node_states.values())

    def pending_approvals(self) -> list[str]:
        return [
            nid for nid, st in self.node_states.items() if st["status"] == WAITING_APPROVAL
        ]

    def all_terminal(self) -> bool:
        return all(st["status"] in TERMINAL for st in self.node_states.values())

    def is_releasable(self) -> bool:
        """§8.6：仅当 ready 集为空时才释放 Worker。"""
        return not self._ready_nodes()

    # ==================================================================
    # 边状态 / 节点决策（§8.2）
    # ==================================================================
    def _edge_active(self, edge) -> bool:
        src = self.node_states.get(edge.source)
        if src is None:
            return False
        # DONE/REJECTED 都产生了输出，可对 when 条件求值（REJECTED → 下游拒绝路径）
        if src["status"] not in (DONE, REJECTED):
            return False  # 未执行 / SKIPPED（输出 None）→ INACTIVE
        if edge.when is not None:
            return bool(eval_condition(edge.when, self.node_states))
        return True

    def _sources_terminal(self, node: Node) -> bool:
        return all(
            self.node_states[e.source]["status"] in TERMINAL for e in node.in_edges
        )

    def _node_decision(self, node: Node) -> str:
        """'ready' | 'skipped' | 'blocked'。"""
        if not node.in_edges:
            return "ready"  # 根节点（无入边）立即可执行
        if node.join == "all":
            required = node.required_edges or [e.source for e in node.in_edges]
            if all(self._edge_active(e) for e in node.in_edges if e.source in required):
                return "ready"
            if self._sources_terminal(node):
                return "skipped"
            return "blocked"
        # join == "any"
        if any(self._edge_active(e) for e in node.in_edges):
            return "ready"
        if self._sources_terminal(node):
            return "skipped"
        return "blocked"

    def _ready_nodes(self) -> list[str]:
        return [
            nid
            for nid, node in self.dag.nodes.items()
            if self.node_states[nid]["status"] == PENDING
            and not node.is_approval
            and self._node_decision(node) == "ready"
        ]

    # ==================================================================
    # 执行
    # ==================================================================
    async def _default_runner(self, node: Node, params: dict) -> Any:
        """默认 mock runner：短延迟 + 结构化输出（无 LLM 时的回退）。"""
        await asyncio.sleep(0.01)
        return {"node": node.id, "ok": True}

    async def _persist(self, nid: str) -> None:
        await self.store.put_node(
            self.run_id, self.tenant_id, nid, self.node_states[nid]
        )

    async def _mark_skipped(self, nid: str) -> None:
        self.node_states[nid] = {"status": SKIPPED, "output": None}
        await self._persist(nid)
        log.info("[%s] skip %s", self.run_id, nid)

    async def _process_skips(self) -> None:
        """标记所有可判定为 SKIPPED 的非审批节点（§8.2.2 skip 级联）。

        ``_ready_nodes()`` 只返回 ready 节点；当某节点所有入边 INACTIVE 且源全部
        终态时（如 approve 拒绝 → test 的 when 不满足），它必须被标记 SKIPPED 终态，
        否则 DAG 永不收敛。审批节点的 skip 已由 _process_approvals 处理。
        """
        for nid, node in self.dag.nodes.items():
            if node.is_approval or self.node_states[nid]["status"] != PENDING:
                continue
            if self._node_decision(node) == "skipped":
                await self._mark_skipped(nid)

    async def _process_approvals(self) -> None:
        """处理可决策的审批节点：when 不满足 → SKIPPED；满足 → WAITING_APPROVAL。"""
        for nid, node in self.dag.nodes.items():
            if not node.is_approval or self.node_states[nid]["status"] != PENDING:
                continue
            decision = self._node_decision(node)
            if decision == "skipped":
                await self._mark_skipped(nid)
                continue
            if decision == "ready":
                params = node.params or {}
                timeout_s = int(params.get("timeout", 3600))
                timeout_at = datetime.now(UTC) + timedelta(seconds=timeout_s)
                self.node_states[nid] = {
                    "status": WAITING_APPROVAL,
                    "output": None,
                    "params": params,
                }
                await self._persist(nid)
                await self.store.create_approval(
                    self.run_id,
                    nid,
                    self.tenant_id,
                    params=params,
                    approvers=list(params.get("approvers", [])),
                    timeout_at=timeout_at.isoformat(),
                )
                log.info("[%s] ⭐ approval %s -> waiting_approval", self.run_id, nid)

    async def _run_with_retry(self, node: Node, params: dict) -> Any:
        """幂等执行 + retry + on_failure 策略（§8.4 / §8.1 on_failure）。"""

        async def invoke() -> Any:
            # runner 约定为 async；兼容同步 runner（脚本化/mock 场景）
            result = self.node_runner(node, params)
            if asyncio.iscoroutine(result):
                return await result
            return result

        async def on_error(exc: Exception) -> Any:
            if node.on_failure == "continue":
                log.info("[%s] %s on_failure=continue，产出负证据", self.run_id, node.id)
                return {"found": False, "error": str(exc)}
            raise WorkflowNodeFailed(node.id, exc) from exc

        return await execute_with_idempotency(
            self.store,
            self.run_id,
            node.id,
            attempt=0,
            action=invoke,
            max_attempts=node.retry + 1,
            on_error=on_error,
        )

    async def _exec_node(self, nid: str) -> None:
        node = self.dag.nodes[nid]
        if self.node_states[nid]["status"] != PENDING:
            return
        decision = self._node_decision(node)
        if decision == "skipped":
            await self._mark_skipped(nid)
            return
        if decision != "ready":
            return

        self.node_states[nid]["status"] = RUNNING
        ctx = {"nodes": self.node_states, "inputs": self.inputs}
        params = resolve_params(node.params, ctx)
        try:
            output = await self._run_with_retry(node, params)
            state: dict = {"status": DONE, "output": output, "params": params}
            # 真实 node_runner（AgentNodeRunner）暴露 last_usage → 合并 token/cost 计量；
            # mock _default_runner 无 last_usage → 保持无 tokens/cost（聚合 GET 诚实 0）
            usage = getattr(self.node_runner, "last_usage", None)
            if usage:
                state["tokens"] = usage.get("tokens", 0)
                state["cost"] = usage.get("cost", 0.0)
            self.node_states[nid] = state
            await self._persist(nid)
            log.info("[%s] done %s", self.run_id, nid)
        except WorkflowNodeFailed as exc:
            self.node_states[nid] = {"status": "failed", "output": None, "error": str(exc)}
            await self._persist(nid)
            self.failed.append(nid)
            raise
        await self._process_approvals()

    async def run(self) -> str:
        """执行到可释放（ready 集为空）。返回 ``done`` / ``waiting_approval`` / ``failed``。

        - 每波并发执行 ready 的 agent 节点；慢分支运行期间 run() 不返回。
        - ready 集为空时：
          * 存在 waiting_approval → 返回 ``waiting_approval``（Worker 释放，§8.6）
          * 全部终态 → 返回 ``done``
          * 节点 failed → 抛出 WorkflowNodeFailed
        """
        while True:
            await self._process_approvals()
            await self._process_skips()
            if self.failed:
                raise WorkflowNodeFailed(self.failed[0], RuntimeError("上游节点失败"))
            ready = self._ready_nodes()
            if ready:
                await asyncio.gather(*(self._exec_node(nid) for nid in ready))
                continue
            if self.has_waiting_approval():
                return "waiting_approval"
            if self.all_terminal():
                return "done"
            # 卡住：无 ready、无 waiting、非全终态
            raise WorkflowStalledError(
                f"run {self.run_id} 停滞：ready 集为空且无审批等待（DAG 死锁？）"
            )

    # ==================================================================
    # 审批（§8.3 CAS + 终态不可逆）
    # ==================================================================
    async def approve(
        self, nid: str, *, approved: bool = True, by: str = "lead-engineer", comment: str = ""
    ) -> dict:
        node = self.dag.nodes[nid]
        assert node.is_approval, f"{nid} 不是审批节点"
        assert self.node_states[nid]["status"] == WAITING_APPROVAL, f"{nid} 不在等待审批"

        aid = f"ap_{self.run_id}_{nid}"
        to_status = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
        ok = await self.store.cas_update_approval(
            aid, APPROVAL_WAITING, to_status, by=by, comment=comment
        )
        if not ok:
            raise ApprovalRaceError(f"审批 {nid} CAS 冲突：已被并发操作推进")

        output = {
            "status": to_status,
            "approved": approved,
            "approver": by,
            "comment": comment,
            "upstream_output": {
                u: self.node_states[u].get("output") for u in node.upstreams
            },
        }
        self.node_states[nid] = {
            "status": DONE if approved else REJECTED,
            "output": output,
            "params": node.params,
        }
        await self._persist(nid)
        log.info("[%s] approval %s -> %s by %s", self.run_id, nid, to_status, by)
        return output

    # ==================================================================
    # 恢复（Resume）
    # ==================================================================
    @classmethod
    async def from_checkpoint(
        cls,
        run_id: str,
        tenant_id: str,
        dag: DAG,
        store: StateStore,
        node_runner: NodeRunner | None = None,
        inputs: dict | None = None,
    ) -> DAGExecutor:
        """从 StateStore 的节点级 checkpoint 重建执行器（§8.4 / §4.4 Resume）。

        - 终态（done/skipped/rejected）回填输出；
        - waiting_approval 保留原状（审批通过后继续，不重复审批）；
        - 其余节点重置为 pending 重新执行。
        """
        ex = cls(run_id, tenant_id, dag, store, node_runner=node_runner, inputs=inputs)
        cps = await store.get_nodes(run_id)
        for nid, cp in cps.items():
            st = dict(cp)
            st.pop("tenant_id", None)
            st.setdefault("output", None)
            st.pop("params", None)  # checkpoint 不存参数，避免陈旧
            if st.get("status") not in (DONE, SKIPPED, WAITING_APPROVAL, REJECTED):
                st = {"status": PENDING, "output": None}
            ex.node_states[nid] = st
        return ex
