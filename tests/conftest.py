"""共享测试 fixture。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """每个测试前重置全局 settings → 测试默认（sqlite/memory/inline）。

    防止本机 .env（如 AGENTFLOW_STATE_STORE=postgres）污染测试——settings 是进程级
    单例（lru_cache），import 时已读 .env，须显式归位。个别测试再按需 monkeypatch。
    """
    from agentflow.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "state_store", "sqlite")
    monkeypatch.setattr(s, "queue", "memory")
    monkeypatch.setattr(s, "run_mode", "inline")
    monkeypatch.setattr(s, "tenants_file", "")
    monkeypatch.setattr(s, "jwt_secret", "")
    monkeypatch.setattr(s, "secret_key", "")
    monkeypatch.setattr(s, "shared_datasources", False)
    monkeypatch.setattr(s, "state_db_path", __import__("pathlib").Path("data/agentflow.db"))
    monkeypatch.setattr(s, "postgres_dsn", "localhost:5432/agentflow?user=agentflow&password=agentflow")


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
