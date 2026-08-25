# -*- coding: utf-8 -*-
"""DAG 拓扑模型 + 静态校验（design §8.2）。

核心语义（§8.2.1 / §8.2.2）：
- **边**：``Edge(source, target, when)``。执行期由 join 引擎计算 ACTIVE / INACTIVE。
  - ``when`` 为 False 的边 → INACTIVE
  - source 节点 SKIPPED 的边 → INACTIVE（SKIPPED 沿正常边级联传播）
  - 其余 → ACTIVE
- **join 策略**（节点级，默认 ``any``）：
  - ``any``：≥1 条 ACTIVE 入边 → READY
  - ``all``：全部 ``required_edges`` 为 ACTIVE → READY
  - 所有入边 INACTIVE → 节点 SKIPPED（终态，输出 None，下游级联）
- 审批节点参与 skip（S-010b 实测）：approval 的 when 不满足 → SKIPPED 而非 WAITING_APPROVAL。

静态校验（§8.2.3）：DAG 环 / 悬空节点 / join 与入边一致性 / params 仅引用传递上游。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

# 节点状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
WAITING_APPROVAL = "waiting_approval"
REJECTED = "rejected"

TERMINAL = {DONE, SKIPPED, REJECTED}

# 边状态
EDGE_ACTIVE = "active"
EDGE_INACTIVE = "inactive"


@dataclass
class Edge:
    source: str
    target: str
    when: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Edge({self.source}->{self.target}{' when='+self.when if self.when else ''})"


@dataclass
class Node:
    id: str
    kind: str = "agent"  # agent | approval
    agent: str | None = None  # agent 节点对应的职能智能体
    in_edges: list[Edge] = field(default_factory=list)
    when: str | None = None  # 便捷写法：单上游时的边条件
    join: str = "any"  # any | all
    required_edges: list[str] = field(default_factory=list)  # all 模式：需要 ACTIVE 的上游
    retry: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    on_failure: str = "abort"  # abort | continue（诊断侧负证据策略）
    on_reject: str = "abort"  # approval 被拒时的行为

    @property
    def is_approval(self) -> bool:
        return self.kind == "approval"

    @property
    def upstreams(self) -> list[str]:
        return [e.source for e in self.in_edges]


class WorkflowDAGError(Exception):
    """DAG 静态校验错误。"""


class DAG:
    """不可变 DAG 拓扑。"""

    def __init__(self, nodes: dict[str, Node], edges: list[Edge]) -> None:
        self.nodes = nodes
        self.edges = edges

    def __len__(self) -> int:
        return len(self.nodes)

    def node_ids(self) -> list[str]:
        return list(self.nodes)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def build(cls, raw_nodes: dict, raw_edges: list[dict] | None = None) -> "DAG":
        """从 YAML 解析后的 ``nodes`` / ``edges`` 构建 DAG。

        ``raw_edges`` 为 None 时回退到节点内联 ``upstreams`` + ``when``（spike 兼容）。
        """
        # 1) 先建节点骨架
        nodes: dict[str, Node] = {}
        for nid, spec in raw_nodes.items():
            spec = dict(spec or {})
            kind = spec.pop("kind", "agent")
            agent = spec.pop("agent", None)
            when = spec.pop("when", None)
            join = spec.pop("join", "any")
            required_edges = spec.pop("required_edges", [])
            retry = int(spec.pop("retry", 0))
            params = spec.pop("params", {}) or {}
            on_failure = spec.pop("on_failure", "abort")
            on_reject = spec.pop("on_reject", "abort")
            if kind == "approval":
                # 审批节点的展示元数据折叠进 params（§8.1：approvers/timeout/name 为同级 key）
                params = {
                    **params,
                    "approvers": spec.pop("approvers", []) or [],
                    "timeout": int(spec.pop("timeout", 3600)),
                    "name": spec.pop("name", nid),
                }
            nodes[nid] = Node(
                id=nid,
                kind=kind,
                agent=agent,
                when=when,
                join=join,
                required_edges=required_edges,
                retry=retry,
                params=params,
                on_failure=on_failure,
                on_reject=on_reject,
            )

        # 2) 边：优先 edges 列表；否则内联 upstreams
        edges: list[Edge] = []
        if raw_edges is not None:
            for e in raw_edges:
                src, tgt = e["from"], e["to"]
                edges.append(Edge(source=src, target=tgt, when=e.get("when")))
        else:
            for nid, spec in raw_nodes.items():
                spec = spec or {}
                upstreams = spec.get("upstreams", []) or []
                if isinstance(upstreams, str):
                    upstreams = [upstreams]
                when = spec.get("when")
                for u in upstreams:
                    edges.append(Edge(source=u, target=nid, when=when))

        # 3) 绑定入边到节点
        for e in edges:
            if e.target not in nodes:
                raise WorkflowDAGError(f"边指向不存在的节点: {e.source}->{e.target}")
            if e.source not in nodes:
                raise WorkflowDAGError(f"边起点不存在的节点: {e.source}->{e.target}")
            nodes[e.target].in_edges.append(e)

        # 3.5) 节点级 `when` 便捷写法：恰有一条未带 when 的入边时，应用到该边
        # （S-010b：approval 的 when 不满足 → SKIPPED；edges-list 与内联两种写法一致）
        for nid, node in nodes.items():
            if node.when and len(node.in_edges) == 1 and node.in_edges[0].when is None:
                node.in_edges[0].when = node.when

        dag = cls(nodes, edges)
        dag._validate()
        return dag

    # ------------------------------------------------------------------
    # 静态校验（§8.2.3）
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        self._check_cycle()
        self._check_join_consistency()

    def _check_cycle(self) -> None:
        """Kahn 拓扑排序检测环。"""
        indeg = {nid: 0 for nid in self.nodes}
        adj: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            adj[e.source].append(e.target)
            indeg[e.target] += 1
        q = deque([nid for nid, d in indeg.items() if d == 0])
        seen = 0
        while q:
            nid = q.popleft()
            seen += 1
            for m in adj[nid]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    q.append(m)
        if seen != len(self.nodes):
            cyclic = [nid for nid, d in indeg.items() if d > 0]
            raise WorkflowDAGError(f"DAG 存在环，涉及节点: {cyclic}")

    def _check_join_consistency(self) -> None:
        for nid, node in self.nodes.items():
            upstreams = node.upstreams
            if node.join == "all":
                if not node.required_edges:
                    raise WorkflowDAGError(
                        f"节点 {nid} 声明 join: all 但未提供 required_edges"
                    )
                missing = [u for u in node.required_edges if u not in upstreams]
                if missing:
                    raise WorkflowDAGError(
                        f"节点 {nid} 的 required_edges 不是其上游: {missing}"
                    )
            elif node.join != "any":
                raise WorkflowDAGError(
                    f"节点 {nid} 的 join 策略非法: {node.join!r}（仅 any/all）"
                )
            elif node.required_edges:
                raise WorkflowDAGError(
                    f"节点 {nid} 使用 join: any 不应指定 required_edges"
                )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def upstream_nodes(self, nid: str) -> list[str]:
        return self.nodes[nid].upstreams

    def downstream_nodes(self, nid: str) -> list[str]:
        return [e.target for e in self.edges if e.source == nid]

    def transitive_upstreams(self, nid: str) -> set[str]:
        """节点所有传递上游（用于校验 params 引用）。"""
        seen: set[str] = set()
        stack = list(self.nodes[nid].upstreams)
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(self.nodes[u].upstreams)
        return seen

    def check_params_refs(self) -> None:
        """§8.2.3(1)：params 只能引用（传递）上游节点的输出。"""
        for nid, node in self.nodes.items():
            allowed = self.transitive_upstreams(nid) | {nid}
            for ref in _collect_param_refs(node.params):
                target = ref.split(".")[0]
                if target not in allowed:
                    raise WorkflowDAGError(
                        f"节点 {nid} 的 params 引用了非上游节点输出: {ref!r}"
                    )


def _collect_param_refs(obj: Any) -> list[str]:
    """递归收集 `$.nodes.X...` 引用中的节点名（X）。"""
    refs: list[str] = []
    if isinstance(obj, str):
        if obj.startswith("$.nodes."):
            refs.append(obj.split(".")[2])
    elif isinstance(obj, dict):
        for v in obj.values():
            refs.extend(_collect_param_refs(v))
    elif isinstance(obj, list):
        for v in obj:
            refs.extend(_collect_param_refs(v))
    return refs
