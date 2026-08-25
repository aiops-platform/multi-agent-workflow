# -*- coding: utf-8 -*-
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
