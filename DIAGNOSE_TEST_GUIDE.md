# diagnose-only 场景：YAML + 三层测试骨架

> 场景：服务报错 → 把 trace 交给分析链路 → 拿到根因结论（`root_cause_type`）。
> 本文档给出：**方案 A 下的最小编排 DAG（`diagnose-only.yaml`）** + **三层测试骨架**。
> 三层分别锁死「编排确定性 / 节点输出契约 / 端到端结果」，测试不断言"LLM 用了哪个工具"。

---

## 1. 工作流定义 `workflows/diagnose-only.yaml`

创建该文件（内容与 `bug-fix-pipeline.yaml` 的诊断段一致，去掉修复/提交部分）：

```yaml
# ===== 仅诊断链路：服务报错 → trace → 根因分析 =====
name: diagnose-only
version: "1.0.0"
description: "最小诊断场景：服务报错 → 取 trace 重建调用链 → 定位根因（无修复/审批）"

inputs:
  bug_report:
    type: object
    required: true
    description: "来自 ServiceNow/Alertmanager 的原始事件（含 trace_id 提示）"

nodes:
  # ---- 诊断阶段（只读；metrics/infra/logs/know 为负证据 on_failure: continue）----
  triage:
    agent: triage
    params: { bug: "$.inputs.bug_report" }
    on_failure: abort

  logs:
    agent: log-analyst
    params: { bug: "$.nodes.triage.output.summary" }
    on_failure: continue

  trace:
    agent: trace-analyst
    params: { bug: "$.nodes.triage.output.summary" }
    on_failure: abort      # trace 是关键证据，失败即中止

  metrics:
    agent: metrics-analyst
    params: { bug: "$.nodes.triage.output.summary" }
    on_failure: continue

  infra:
    agent: infra-locator
    params: { bug: "$.nodes.triage.output.summary" }
    on_failure: continue

  locate:
    agent: code-locator
    params:
      bug: "$.nodes.triage.output.summary"
      target_service: "$.nodes.trace.output.failing_service"   # ← 依赖 trace 的根因服务
    on_failure: abort

  know:
    agent: knowledge-lookup
    params: { bug: "$.nodes.triage.output.summary" }
    on_failure: continue

  rca:
    agent: root-cause
    params:
      logs: "$.nodes.logs.output.summary"
      trace: "$.nodes.trace.output.summary"
      metrics: "$.nodes.metrics.output.summary"
      infra: "$.nodes.infra.output.summary"
      code: "$.nodes.locate.output.summary"
      know: "$.nodes.know.output.summary"
    on_failure: abort

edges:
  - { from: triage, to: logs }
  - { from: triage, to: trace }
  - { from: triage, to: metrics }
  - { from: triage, to: infra }
  - { from: triage, to: locate }
  - { from: triage, to: know }
  - { from: trace, to: locate }
  - { from: logs, to: rca }
  - { from: trace, to: rca }
  - { from: metrics, to: rca }
  - { from: infra, to: rca }
  - { from: locate, to: rca }
  - { from: know, to: rca }
```

> 语义：`triage` 先出初步判断 → `logs/trace/metrics/infra/locate/know` 并行取证
> （`asyncio.gather`）→ `rca` 汇合出根因。`locate` 的 `target_service` 由 `trace` 的输出
> 注入（参数引用只允许上游，`core/dag.py` 静态校验）。

---

## 2. 三层测试骨架

### 第①层：编排语义测试（无 LLM，进 CI，秒级）`tests/test_diagnose_chain.py`

```python
"""编排语义测试：锁死 DAG 结构、参数传递、join/skip、负证据——不调用大模型。"""
from pathlib import Path

from agentflow.core.dag import DONE
from agentflow.core.workflow import Workflow
from agentflow.service import RunService
from agentflow.statestore.memory import InMemoryStateStore

WF = Path(__file__).resolve().parent.parent / "workflows" / "diagnose-only.yaml"

BUG = {
    "number": "INC0012345",
    "short_description": "订单服务结账无响应",
    "cmdb_ci": {"name": "order-service", "namespace": "order"},
    "correlation_hint": {"trace_id": "trace-20260819-143000-abc123"},
}


def scripted_runner(node, params):
    """确定性输出：只回填当前节点的契约字段，覆盖诊断链各消费点。"""
    a = node.agent
    if a == "triage":
        return {"symptom_type": "crash", "severity": "high",
                "summary": "结账无响应，疑似下游服务故障"}
    if a == "log-analyst":
        return {"error_type": None, "summary": "未见明显应用错误"}
    if a == "trace-analyst":
        return {"failing_service": "warranty-service", "first_error": "fin must not be null",
                "summary": "故障 span 在下游 warranty-service"}
    if a == "metrics-analyst":
        return {"anomalies": [], "summary": "资源指标正常"}
    if a == "infra-locator":
        return {"status": "Running", "summary": "Pod 正常"}
    if a == "code-locator":
        return {"service": "warranty-service",
                "repo_url": "https://github.com/company/warranty-service",
                "suspicious_files": ["src/main/java/WarrantyService.java"],
                "summary": "定位到 WarrantyService"}
    if a == "knowledge-lookup":
        return {"found": False, "summary": "无历史相似事件"}
    if a == "root-cause":
        return {"root_cause_type": "code_bug", "confidence": 0.9,
                "failing_service": "warranty-service",
                "summary": "warranty fin 缺参导致结账挂起"}
    return {"node": node.id, "ok": True}


async def test_chain_converges_done():
    """全链收敛：所有节点终态 = done（diagnose-only 无审批节点）。"""
    wf = Workflow.load_yaml(WF)
    svc = RunService(InMemoryStateStore(), node_runner=scripted_runner)
    summary = await svc.create_run("tenant-a", wf, {"bug_report": BUG})
    assert all(st == DONE for st in summary["status"].values()), summary


async def test_param_from_trace_to_locate():
    """参数传递：locate.target_service 正确收到 trace.failing_service。"""
    seen: dict = {}

    def runner(node, params):
        seen[node.id] = dict(params)
        return scripted_runner(node, params)

    wf = Workflow.load_yaml(WF)
    svc = RunService(InMemoryStateStore(), node_runner=runner)
    await svc.create_run("tenant-a", wf, {"bug_report": BUG})
    assert seen["locate"]["target_service"] == "warranty-service"


async def test_negative_evidence_continues():
    """负证据：log-analyst 失败（on_failure: continue）不中断，链仍收敛。"""

    def runner(node, params):
        if node.id == "logs":
            raise RuntimeError("ES 查询失败")
        return scripted_runner(node, params)

    wf = Workflow.load_yaml(WF)
    svc = RunService(InMemoryStateStore(), node_runner=runner)
    summary = await svc.create_run("tenant-a", wf, {"bug_report": BUG})
    # logs 产出负证据后仍为 DONE；全链收敛
    assert summary["status"]["logs"]["status"] == DONE
    assert all(st == DONE for st in summary["status"].values()), summary
```

### 第②层：节点契约测试（录制数据源 + 真实模型）`tests/test_diagnose_nodes.py`

```python
"""节点契约测试：录制 trace fixture + 真实 DeepSeek，断言输出契约（需 DEEPSEEK_API_KEY）。"""
import pytest

from agentflow.agents.mcp import build_toolkit
from agentflow.agents.scopes import build_agent, build_model, run_agent
from agentflow.config import get_settings

# 从真实 testbed 导出的 trace fixture —— 固定输入，输出才可重复断言
RECORDED_TRACE = {
    "trace_id": "trace-20260819-143000-abc123",
    "chain": [
        {"service": "order-service", "has_error": True, "completed": False,
         "first_error": "feign.FeignException: Read timed out"},          # 下游调用症状
        {"service": "warranty-service", "has_error": True, "completed": False,
         "first_error": "java.lang.IllegalStateException: fin must not be null"},  # 业务根因
    ],
    "failing_service": "warranty-service",
    "summary": "trace 重建 42 条日志 / 2 个服务，故障 span 疑似 warranty-service",
}


class RecordedDataSource:
    """录制数据源：签名与 RealDataSourceAdapter 一致，只返回固定 fixture。"""

    async def get_trace(self, trace_id=None, service=None, minutes=15, limit=100):
        return RECORDED_TRACE

    async def query_logs(self, service=None, level=None, trace_id=None, limit=50):
        return {
            "found": True, "count": 1,
            "logs": [{"@timestamp": "2026-08-19T14:31:00+08:00", "level": "ERROR",
                      "service": "warranty-service", "trace_id": RECORDED_TRACE["trace_id"],
                      "message": "java.lang.IllegalStateException: fin must not be null"}],
            "summary": "ES 返回 1 条日志（warranty-service, ERROR）",
        }


@pytest.mark.skipif(not get_settings().deepseek_api_key, reason="缺 DEEPSEEK_API_KEY")
async def test_trace_analyst_root_cause_contract():
    """trace-analyst 在固定 trace 下：输出契约成立、根因服务命中预期。"""
    toolkit = build_toolkit("trace-analyst", use_mock=True, datasource=RecordedDataSource())
    # max_iters ≥ 12：trace-analyst 需要（2 工具 + 链合成，默认 6 会耗尽返回 {}）
    agent = build_agent("trace-analyst", toolkit, build_model(), max_iters=12)
    out = await run_agent(agent, {"bug": {"trace_id": RECORDED_TRACE["trace_id"]}})

    assert isinstance(out, dict)                       # LLM 输出不合契约 → {} → 断言失败
    assert "failing_service" in out                    # 契约字段存在
    assert out.get("failing_service") == "warranty-service"
    assert out.get("root_cause_type") in ("code_bug", "infra_issue")
    assert "warranty-service" in (out.get("summary") or "")   # 子串断言（S-011 风格）
```

### 第③层：E2E 验收（真实模型 + 真实 testbed）`tests/e2e/test_diagnose_e2e.py`

```python
"""端到端验收：注入故障 → 真实 DeepSeek + 真实 ES/Prometheus/kubectl → 断言根因类型。
等价于 scripts/diagnose_scenario1.py / diagnose_scenario2.py，默认跳过（需 testbed）。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"), reason="需 RUN_E2E=1 + testbed 就绪"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


async def test_scenario_disk_full_infra_issue():
    """场景1：磁盘打满 → 期望 root_cause_type == infra_issue。

    前置：已注入故障 + port-forward 完成（见 README「testbed 真实联调」）。
    """
    proc = subprocess.run(
        [sys.executable, "scripts/diagnose_scenario1.py"],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    # diagnose_scenario1.py 打印的根因结论需命中 infra_issue
    assert "infra_issue" in proc.stdout, proc.stdout


async def test_scenario2_warranty_code_bug():
    """场景2：warranty fin 缺参 → 期望 root_cause_type == code_bug。"""
    proc = subprocess.run(
        [sys.executable, "scripts/diagnose_scenario2.py"],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    assert "code_bug" in proc.stdout, proc.stdout
```

---

## 3. 运行方式

```bash
cd <repo>

# 第①层：无 LLM，秒级，进 CI
./venv/bin/pytest tests/test_diagnose_chain.py -q

# 第②层：真实模型（需要 DEEPSEEK_API_KEY，否则自动 skip）
DEEPSEEK_API_KEY=sk-xxx ./venv/bin/pytest tests/test_diagnose_nodes.py -q

# 第③层：端到端（需要 testbed 注入故障 + port-forward，见 README 联调章节）
RUN_E2E=1 ./venv/bin/pytest tests/e2e/test_diagnose_e2e.py -q
```

> 若尚未创建 `workflows/diagnose-only.yaml`，第①层也可改为直接内联加载：
> `Workflow.load_yaml("{ 上方的 YAML 文本 }")`（`load_yaml` 支持内联 YAML 字符串）。

---

## 4. 这套骨架与现有代码的对应关系

| 骨架里的东西 | 对应现有实现 | 说明 |
|---|---|---|
| `scripted_runner` | `executor/dag_executor.py:_default_runner` / `demo.py:_scripted_runner` | 无 LLM 的确定性节点执行 |
| 录制数据源 | `agents/datasources.py:RealDataSourceAdapter`（真实）/ `agents/tools.py:MOCK_L1_TOOLS`（mock） | 签名一致，换 adapter 即可 |
| `build_toolkit` | `agents/mcp.py:build_toolkit` | L1 只读 + L2 执行按 agent 装配 |
| `build_model/build_agent/run_agent` | `agents/scopes.py` | AgentScope 2.0.3 适配 |
| 工具白名单 | `agents/tools.py:TOOL_REGISTRY` | trace-analyst 只见 `get_trace`/`query_logs` |
| `max_iters=12` | `scripts/diagnose_scenario{1,2}.py` 已有此设置 | trace-analyst 迭代要求 |
