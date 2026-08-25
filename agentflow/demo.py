# -*- coding: utf-8 -*-
"""脚本化 demo：跑通 bug-fix-pipeline 全链路（诊断 → 修复 → 合并审批 → 测试 → 提交）。

使用确定性 mock runner（不调用真实 LLM），验证编排语义；真实 LLM 接入见
``agentflow/agents/scopes.py:build_model``（M1）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .core.dag import DONE, SKIPPED, WAITING_APPROVAL
from .core.workflow import Workflow
from .service import RunService
from .statestore.memory import InMemoryStateStore

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / "workflows" / "bug-fix-pipeline.yaml"

_BUG_REPORT = {
    "number": "INC0012345",
    "short_description": "订单服务报价单打印失败",
    "description": "业务人员点击「打印报价单」后长时间无响应，最终提示下载失败。",
    "cmdb_ci": {"name": "order-service", "namespace": "order"},
    "correlation_hint": {"trace_id": "trace-20260819-143000-abc123"},
}


def _scripted_runner(node, params: dict):
    """确定性输出，覆盖 §8.1 各节点消费的字段。"""
    out = {"node": node.id}
    agent = node.agent or ""
    if agent == "triage":
        out.update(symptom_type="crash", severity="high", summary="订单服务报价单打印失败，疑似基础设施故障")
    elif agent == "log-analyst":
        out.update(error_type="IOException", error_message="No space left on device", summary="日志显示磁盘空间不足")
    elif agent == "trace-analyst":
        out.update(failing_service="warranty-service", first_error="fin must not be null", summary="故障 span 在下游 warranty-service")
    elif agent == "metrics-analyst":
        out.update(anomalies=["cpu_100", "disk_100"], summary="CPU 与磁盘均打满")
    elif agent == "infra-locator":
        out.update(pod_name="order-service-7b9c8d5f6-abcde", status="OOMKilled", summary="磁盘与内存压力")
    elif agent == "code-locator":
        out.update(service="warranty-service", repo_url="https://github.com/company/warranty-service",
                   suspicious_files=["src/main/java/FinCalculator.java"], summary="定位到 FinCalculator")
    elif agent == "knowledge-lookup":
        out.update(found=True, similar_incidents=["INC0001"], summary="历史故障：磁盘写满导致文件生成失败")
    elif agent == "root-cause":
        out.update(root_cause_type="infra_issue", confidence=0.97,
                   hypotheses=["磁盘打满", "代码 temp 文件泄漏"],
                   ruled_out=["network", "dns"], summary="根因：磁盘 100% + 重试风暴")
    elif agent == "fix-planner":
        out.update(plan={"summary": "清盘止血 + 修 temp 文件泄漏", "steps": [
            {"type": "infra_action", "target": "order-service", "action": "scale + 清 /tmp", "expected": "恢复"},
            {"type": "code_fix", "target": "QuotationService.java", "action": "finally 清理临时文件", "expected": "防复发"},
        ]})
    elif agent == "fix-implementer":
        out.update(diff="--- a/QuotationService.java\n+++ b/QuotationService.java\n-finally {}\n+finally { temp.delete(); }",
                   files_changed=["QuotationService.java"], explanation="临时文件路径 finally 未清理")
    elif agent == "infra-remediator":
        out.update(changes=[{"action": "scale_deployment", "namespace": "order", "params": {"target_replicas": 3}}])
    elif agent == "tester":
        out.update(passed=True, tests_run=12, failed=[])
    elif agent == "reviewer":
        out.update(approved=True, comments=["合理"], risk="low")
    elif agent == "committer":
        out.update(pr_url="https://github.com/company/warranty-service/pull/42", pr_number=42, base_sha="abc123")
    elif agent == "postmortem":
        out.update(summary="磁盘打满引发重试风暴，已清盘并修代码防复发", root_cause="磁盘 100%")
    return out


async def main() -> None:
    wf = Workflow.load_yaml(WORKFLOW_PATH)
    print(f"Workflow: {wf.name} v{wf.version} | hash={wf.workflow_hash[:12]} | 节点 {len(wf.dag)}")

    svc = RunService(InMemoryStateStore(), node_runner=_scripted_runner)
    summary = await svc.create_run("team-alpha", wf, {"bug_report": _BUG_REPORT})
    print("\n[create_run] 诊断 → 修复 → 合并审批 挂起")
    for nid, st in summary["status"].items():
        print(f"  {nid:16s} {st}")

    res = await svc.approve(summary["run_id"], "approve-changes", approved=True, by="lead-engineer", comment="方案合理")
    print("\n[approve] 通过 → 测试 → 审查 → 提交审批 挂起")
    for nid, st in res["status"].items():
        print(f"  {nid:16s} {st}")

    final = await svc.approve(res["run_id"], "approve-commit", approved=True, by="lead-engineer")
    print("\n[approve] 通过 → 提交 → 复盘")
    for nid, st in final["status"].items():
        print(f"  {nid:16s} {st}")
    print("\n✅ 全链路完成:", "DONE" if final["run_status"] == "done" else final["run_status"])


if __name__ == "__main__":
    asyncio.run(main())
