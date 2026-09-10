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


class NodeInputError(Exception):
    """节点必填入参（``require``）缺失或解析到非法值。

    输入有问题应直接失败，而不是把坏输入交给 agent 空转（用户踩过：requestId 为 null
    时 app-log 卡死无任何输出）。on_failure: abort → 整条链置 failed；continue → 负证据。
    """

    def __init__(self, node_id: str, missing: list[str]) -> None:
        super().__init__(
            f"节点 {node_id} 入参缺失/非法，未满足 require {missing}——输入有问题直接判失败"
        )
        self.node_id = node_id
        self.missing = missing


def _usable(value: Any) -> bool:
    """入参是否"可用"：非 None、非空串、非空 list/dict。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


class WorkflowStalledError(Exception):
    """ready 集为空、无 waiting_approval、又未全部终态 —— DAG 死锁或逻辑错误。"""


class ApprovalRaceError(Exception):
    """审批 CAS 冲突（已被并发操作推进到终态）。"""


_FALLBACK_SEP = " || "  # 参数引用回退分隔符：$.a || $.b → a 解析不到可用值则取 b（新旧 ticket 契约兼容）


def _path_steps(path: str) -> list[Any]:
    """把点分路径拆成步（含 ``[idx]`` 下标）：``a.b[0].c[1][2]`` → ['a','b',0,'c',1,2]。"""
    steps: list[Any] = []
    i, n = 0, len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            i += 1
            continue
        if ch == "[":
            j = path.index("]", i)
            try:
                steps.append(int(path[i + 1 : j]))
            except ValueError:
                return []
            i = j + 1
            continue
        j = i
        while j < n and path[j] not in ".[":
            j += 1
        steps.append(path[i:j])
        i = j
    return steps


def _walk(root: Any, path: str) -> Any:
    """沿点分路径 + 下标下钻；任一步失配（dict 无键 / list 越界）返回 None。"""
    cur = root
    for step in _path_steps(path):
        if isinstance(cur, dict) and isinstance(step, str) and step in cur:
            cur = cur[step]
        elif isinstance(cur, (list, tuple)) and isinstance(step, int):
            if -len(cur) <= step < len(cur):
                cur = cur[step]
            else:
                return None
        else:
            return None
    return cur


def _resolve_param(value: Any, ctx: dict) -> Any:
    """解析 `$.nodes.X.output[.field]` / `$.inputs.X` 引用；普通值原样返回。

    支持数组下标：``$.inputs.correlation_hint.sample_trace_ids[0]`` /
    ``$.nodes.X.output.key_logs[0].msg``。
    支持回退：``$.inputs.requestId || $.inputs.correlation_hint.sample_trace_ids[0]``
    —— 前分支解析不到可用值（None/空串/空容器，同 require 语义）时取后分支。
    """
    if isinstance(value, str) and value.startswith("$."):
        if _FALLBACK_SEP in value:
            branches = [b.strip() for b in value.split(_FALLBACK_SEP)]
            if all(b.startswith("$.") for b in branches):
                for branch in branches:
                    got = _resolve_param(branch, ctx)
                    if _usable(got):
                        return got
                return None
        path = value[2:]  # 去掉 "$."
        if path.startswith("nodes."):
            rest = path[len("nodes."):]
            node_id = rest.split(".", 1)[0]  # 节点名不带下标/点
            field = rest.split(".", 1)[1] if "." in rest else ""
            node_output = ctx["nodes"].get(node_id, {}).get("output")
            if node_output is None:
                return None
            # "output" 是标准访问器（取节点输出值），不是节点输出的字段
            if field == "output":
                return node_output
            if field.startswith("output"):
                field = field.removeprefix("output")  # ".field" 或 "[0]" 或 ".key_logs[0].msg"
            if not field:
                return node_output
            return _walk(node_output, field)
        if path.startswith("inputs."):
            return _walk(ctx.get("inputs", {}), path[len("inputs."):])
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

    async def _flush_node_trace(self, nid: str) -> None:
        """把该节点成功后取到的明细行落 ``node_traces``，并派生审计（§9.5）。

        - 仅真实 ``AgentNodeRunner`` 有明细（``take_trace``）；mock runner 直接返回。
        - 明细行 = node 汇总 + llm_call + tool_call + denied；DB/审计失败只记日志不翻车。
        - tool_call → 审计 ALLOW；denied → 审计 DENY（input 经 ``mask_input`` 脱敏）。
        """
        if not hasattr(self.node_runner, "take_trace"):
            return
        node = self.dag.nodes[nid]
        try:
            rows = self.node_runner.take_trace(node)
            if not rows:
                return
            await self.store.replace_node_traces(
                self.run_id, nid, self.tenant_id, rows=rows
            )
            from ..audit.logger import mask_input

            for r in rows:
                kind = r["kind"]
                if kind not in ("tool_call", "denied"):
                    continue
                decision = "ALLOW" if kind == "tool_call" else "DENY"
                p = r.get("payload", {})
                inp = p.get("input")
                await self.store.append_audit(
                    self.tenant_id,
                    tool_name=r.get("name") or "unknown",
                    decision=decision,
                    run_id=self.run_id,
                    node_id=nid,
                    input_masked=mask_input(inp) if inp is not None else None,
                    actor=node.agent,
                )
        except Exception:  # noqa: BLE001
            log.exception("[%s] flush node_trace %s 失败（忽略，不影响 run）", self.run_id, nid)

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
        """幂等执行 + retry + on_failure 策略（§8.4 / §8.1 on_failure）。

        - **入参预检**：``node.require`` 里的键解析后不可用（None/空串/空容器）→ 立即
          走 on_error（不调 agent、不空转）。输入有问题直接失败（NodeInputError）。
        - **可选墙钟上限**：``node.timeout``（秒）存在时，单次尝试用 ``asyncio.wait_for``
          限时，超时按节点失败处理（防止网络/模型侧无限等待而整条链卡死）。
        """

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

        # ── 入参预检：输入有问题 → 立即失败，而非交给 agent 空转 ──
        missing = [k for k in node.require if not _usable(params.get(k))]
        if missing:
            return await on_error(NodeInputError(node.id, missing))

        async def action() -> Any:
            if node.timeout:
                try:
                    return await asyncio.wait_for(invoke(), timeout=node.timeout)
                except TimeoutError:
                    raise TimeoutError(
                        f"节点 {node.id} 执行超时（>{node.timeout}s 未完成，输入合法但执行停滞），判定失败"
                    ) from None
            return await invoke()

        return await execute_with_idempotency(
            self.store,
            self.run_id,
            node.id,
            attempt=0,
            action=action,
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
        # 节点开始即落 running：GET /runs/{id} 实时读库 → 前端能看到「执行中」（running 样式已就绪）。
        # 终态 _persist 整 dict 覆盖本行；crash 遗留的 running 行由 resume/重跑覆盖。
        self.node_states[nid]["params"] = params
        await self._persist(nid)
        try:
            output = await self._run_with_retry(node, params)
            state: dict = {"status": DONE, "output": output, "params": params}
            # 真实 node_runner（AgentNodeRunner）暴露 take_usage → 合并 token/cost 计量
            # （按节点 pop，防并行 agent 波串扰）；mock _default_runner 无该方法 → 保持无计量
            usage = (
                self.node_runner.take_usage(node)
                if hasattr(self.node_runner, "take_usage")
                else None
            )
            if usage:
                state["tokens"] = usage.get("tokens", 0)
                state["cost"] = usage.get("cost", 0.0)
            self.node_states[nid] = state
            await self._persist(nid)
            # 节点成功后才把明细落 node_traces + 派生审计（失败只记日志，不影响 run）
            await self._flush_node_trace(nid)
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
