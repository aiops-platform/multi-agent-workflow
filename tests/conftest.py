# -*- coding: utf-8 -*-
"""共享测试 fixture。"""
from __future__ import annotations

import pytest

# 用系统 Python 全局已装的 agentscope==2.0.3，避免测试因缺依赖而挂（见 pyproject）
from agentflow.core.dag import DAG

SIMPLE_YAML = """
name: simple
version: "1.0.0"
inputs:
  bug: { type: string }
nodes:
  a:
    agent: triage
    params: { bug: "$.inputs.bug" }
  b:
    agent: log-analyst
    params: { bug: "$.nodes.a.output.summary" }
  c:
    agent: root-cause
    params: { logs: "$.nodes.b.output.summary" }
edges:
  - { from: a, to: b }
  - { from: b, to: c }
"""

# 含并行 + 条件边 + skip 的拓扑（对应 §8.2 语义）
PARALLEL_YAML = """
name: parallel
version: "1.0.0"
inputs: {}
nodes:
  triage:
    agent: triage
  logs:
    agent: log-analyst
  trace:
    agent: trace-analyst
  rca:
    agent: root-cause
    join: all
    required_edges: [logs, trace]
  approve:
    kind: approval
    approvers: ["lead"]
    timeout: 3600
    params: { diff: "$.nodes.trace.output" }
  test:
    agent: tester
    when: "$.nodes.approve.output.approved == true"
  recap:
    agent: postmortem
    when: "$.nodes.approve.output.approved == false"
edges:
  - { from: triage, to: logs }
  - { from: triage, to: trace }
  - { from: logs, to: rca }
  - { from: trace, to: rca }
  - { from: rca, to: approve }
  - { from: approve, to: test, when: "$.nodes.approve.output.approved == true" }
  - { from: approve, to: recap, when: "$.nodes.approve.output.approved == false" }
"""


@pytest.fixture
def simple_dag() -> DAG:
    from agentflow.core.workflow import Workflow

    return Workflow.load_yaml(SIMPLE_YAML).dag


@pytest.fixture
def parallel_dag() -> DAG:
    from agentflow.core.workflow import Workflow

    return Workflow.load_yaml(PARALLEL_YAML).dag
