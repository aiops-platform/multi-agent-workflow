# -*- coding: utf-8 -*-
"""场景1 真实联调：DeepSeek 模型 + 真实 testbed 数据源，跑诊断链。

用法（先部署 testbed + 注入 scenario1 故障）：
    DEEPSEEK_API_KEY=... ./venv/bin/python scripts/diagnose_scenario1.py

诊断链：triage → log-analyst → metrics-analyst → infra-locator → root-cause。
每个 agent 的工具绑定真实数据源（ES/Prometheus/kubectl），SCENARIOS §5.2。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 允许直接运行 scripts/diagnose_scenario1.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.agents.datasources import RealDataSourceAdapter  # noqa: E402
from agentflow.agents.mcp import build_toolkit  # noqa: E402
from agentflow.agents.scopes import build_agent, build_model, run_agent  # noqa: E402

# SCENARIOS §2.2 场景1 bug report（模拟 ServiceNow ticket）
BUG_REPORT = {
    "number": "INC0012345",
    "short_description": "订单服务报价单打印失败",
    "description": "业务人员点击「打印报价单」后长时间无响应，最终提示下载失败、无法生成报价单文件。影响全部业务人员，已持续 10 分钟。",
    "category": "Application",
    "subcategory": "order-service",
    "impact": "1 - High",
    "urgency": "1 - High",
    "priority": "P1",
    "state": "New",
    "cmdb_ci": {"name": "order-service", "service": "订单服务", "namespace": "order"},
    "correlation_hint": {"first_error": "java.io.IOException: No space left on device"},
}

AGENTS = ["triage", "log-analyst", "metrics-analyst", "infra-locator", "root-cause"]


async def main() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("缺 DEEPSEEK_API_KEY（可 source spike/.env）")
    ds = RealDataSourceAdapter()
    model = build_model()
    print(f"模型: {model.model} | 数据源: ES/ Prometheus/kubectl（testbed）\n")

    evidence: dict[str, str] = {}
    for name in AGENTS:
        toolkit = build_toolkit(name, use_mock=True, datasource=ds)
        agent = build_agent(name, toolkit, model)
        # 输入：triage 用 bug_report，其余把上一环证据传给 agent
        user_input = {"bug": BUG_REPORT} if name == "triage" else {"bug": BUG_REPORT, "evidence": evidence}
        out = await run_agent(agent, user_input)
        evidence[name] = out
        print(f"[{name}]")
        print("  ", json.dumps(out, ensure_ascii=False))
        print()

    rca = evidence.get("root-cause", {})
    rca_type = rca.get("root_cause_type")
    conf = rca.get("confidence")
    print("=" * 50)
    print(f"根因类型: {rca_type}  置信度: {conf}")
    if rca_type == "infra_issue":
        print("✅ 场景1 期望根因 infra_issue（磁盘/CPU 打满）—— 命中")
    else:
        print(f"⚠️ 根因类型为 {rca_type}，与场景1 期望（infra_issue）不符")
    await ds.aclose()


if __name__ == "__main__":
    asyncio.run(main())
