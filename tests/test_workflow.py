"""M0：Workflow 加载 + 版本冻结 + 静态校验（design §8.1 / §8.2.3 / §8.5）。"""
from __future__ import annotations

import pytest

from agentflow.core.dag import WorkflowDAGError
from agentflow.core.workflow import Workflow

from .conftest import SIMPLE_YAML


def test_load_yaml_basic() -> None:
    wf = Workflow.load_yaml(SIMPLE_YAML)
    assert wf.name == "simple"
    assert set(wf.dag.node_ids()) == {"a", "b", "c"}
    assert wf.dag.upstream_nodes("c") == ["b"]
    assert wf.dag.upstream_nodes("b") == ["a"]


def test_snapshot_hash_stable() -> None:
    wf1 = Workflow.load_yaml(SIMPLE_YAML)
    wf2 = Workflow.load_yaml(SIMPLE_YAML)
    # 同一 YAML → 同一 hash（§8.5 幂等 snapshot 复用）
    assert wf1.workflow_hash == wf2.workflow_hash
    assert len(wf1.workflow_hash) == 64  # sha256


def test_snapshot_contains_full_yaml() -> None:
    wf = Workflow.load_yaml(SIMPLE_YAML)
    snap = wf.snapshot()
    assert snap["workflow_name"] == "simple"
    assert "nodes:" in snap["workflow_yaml"]
    assert "bug-fix-pipeline" not in snap["workflow_yaml"]  # 只含本 workflow


def test_rejects_cycle() -> None:
    raw = {
        "nodes": {"a": {}, "b": {}},
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    with pytest.raises(WorkflowDAGError, match="环"):
        Workflow.load_yaml(raw)


def test_rejects_dangling_edge_target() -> None:
    raw = {"nodes": {"a": {}}, "edges": [{"from": "a", "to": "ghost"}]}
    with pytest.raises(WorkflowDAGError, match="不存在"):
        Workflow.load_yaml(raw)


def test_rejects_param_ref_non_upstream() -> None:
    """§8.2.3(1)：params 只能引用（传递）上游节点输出。"""
    raw = {
        "nodes": {
            "a": {"params": {"x": "$.nodes.c.output"}},
            "b": {},
            "c": {},
        },
        "edges": [{"from": "b", "to": "a"}],
    }
    with pytest.raises(WorkflowDAGError, match="非上游"):
        Workflow.load_yaml(raw)


def test_join_all_requires_required_edges() -> None:
    raw = {
        "nodes": {"a": {}, "b": {"join": "all"}},
        "edges": [{"from": "a", "to": "b"}],
    }
    with pytest.raises(WorkflowDAGError, match="required_edges"):
        Workflow.load_yaml(raw)


def test_join_any_forbids_required_edges() -> None:
    raw = {
        "nodes": {"a": {}, "b": {"join": "any", "required_edges": ["a"]}},
        "edges": [{"from": "a", "to": "b"}],
    }
    with pytest.raises(WorkflowDAGError, match="不应指定"):
        Workflow.load_yaml(raw)


# ----------------------------------------------------------------------
# on_reject 与「驳回边」的一致性（§8.1 / CLAUDE.md 约束 4.1）
# ----------------------------------------------------------------------
def test_on_reject_abort_with_reject_edge_is_rejected_at_load() -> None:
    """`abort` + `approved == false` 出边 = 矛盾，**加载期就该报错**。

    这不是风格问题：`abort` 下驳回会直接抛 `WorkflowNodeFailed` 中止整条 run，
    那条边**永远不可达**——图看着有去处、实际是死的，而**加载与运行都不报错**。

    实测踩过（2026-09-18）：`scripts/problem-log-diagnose.workflow.yaml` 同时写了两个，
    作者基于一句**已失效 14.5 小时的注释**（"on_reject 只解析、从不消费"）以为
    `abort` 是无害的默认值。是测试红了才发现的。
    """
    raw = {
        "nodes": {
            "a": {"agent": "triage"},
            # 不写 on_reject → 默认 abort
            "appr": {"kind": "approval", "approvers": ["lead"]},
            "recap": {"agent": "postmortem"},
        },
        "edges": [
            {"from": "a", "to": "appr"},
            {"from": "appr", "to": "recap",
             "when": "$.nodes.appr.output.approved == false"},
        ],
    }
    with pytest.raises(WorkflowDAGError, match="永远不可达"):
        Workflow.load_yaml(raw)


def test_on_reject_abort_with_reject_edge_via_node_when_is_also_rejected() -> None:
    """驳回路由写成**节点级 `when`**（单上游的便捷写法）时同样要拦。

    便捷写法在 DAG 构建期会变成边条件，校验必须看得见——否则换个写法就绕过去了。

    ⚠️ 构造要点：`upstreams` 只在**没有 edges 列表**时才生效
    （`DAG.build` 的 "raw_edges 为 None 时回退到内联 upstreams"）。
    写了 `edges:` 又指望 `upstreams` 建边，会得到一张**空的**入边表——
    校验当然抓不到（我第一版就这么写的，白红了一次）。
    """
    raw = {
        "nodes": {
            "a": {"agent": "triage"},
            "appr": {"kind": "approval", "approvers": ["lead"], "upstreams": ["a"]},
            "recap": {"agent": "postmortem", "upstreams": ["appr"],
                      "when": "$.nodes.appr.output.approved == false"},
        },
        # 刻意不给 edges：走内联 upstreams 这条路
    }
    with pytest.raises(WorkflowDAGError, match="永远不可达"):
        Workflow.load_yaml(raw)


def test_on_reject_abort_without_reject_edge_loads() -> None:
    """`abort` + 只有 `approved == true` 边 → 合法（这才是 abort 的正常形状）。"""
    raw = {
        "nodes": {
            "a": {"agent": "triage"},
            "appr": {"kind": "approval", "approvers": ["lead"], "on_reject": "abort"},
            "commit": {"agent": "committer"},
        },
        "edges": [
            {"from": "a", "to": "appr"},
            {"from": "appr", "to": "commit",
             "when": "$.nodes.appr.output.approved == true"},
        ],
    }
    assert Workflow.load_yaml(raw).dag.nodes["appr"].on_reject == "abort"


def test_on_reject_continue_without_reject_edge_still_loads() -> None:
    """**反方向刻意不查**：`continue` 而没有驳回边是合法的。

    语义是「驳回后下游全部失活、run 照常收敛（不中止）」——与 `abort` 的区别正在于此。
    一开始我把这一向也写成报错，打掉了 4 个既有测试（`test_run_api.py` 的
    APPROVAL_YAML 就是这个形态，注释写明了意图）才想明白。
    这条测试钉住"别把它加回去"。
    """
    raw = {
        "nodes": {
            "a": {"agent": "triage"},
            "appr": {"kind": "approval", "approvers": ["lead"],
                     "on_reject": "continue"},
            "commit": {"agent": "committer"},
        },
        "edges": [
            {"from": "a", "to": "appr"},
            {"from": "appr", "to": "commit",
             "when": "$.nodes.appr.output.approved == true"},
        ],
    }
    assert Workflow.load_yaml(raw).dag.nodes["appr"].on_reject == "continue"
