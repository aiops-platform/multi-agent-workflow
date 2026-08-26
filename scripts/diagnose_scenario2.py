# -*- coding: utf-8 -*-
"""场景2 真实联调：DeepSeek + 真实 testbed 数据源，跑诊断链（跨服务代码故障）。

用法（先部署 testbed + 注入 scenario2 故障）：
    DEEPSEEK_API_KEY=... ./venv/bin/python scripts/diagnose_scenario2.py

诊断链：triage → log-analyst → trace-analyst（核心）→ metrics/infra（负证据）
       → code-locator（CMDB 多仓库）→ knowledge-lookup → root-cause。
验证目标：root_cause_type=code_bug，故障服务=warranty-service（SCENARIOS §3）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.agents.datasources import RealDataSourceAdapter  # noqa: E402
from agentflow.agents.mcp import build_toolkit  # noqa: E402
from agentflow.agents.scopes import build_agent, build_model, run_agent  # noqa: E402
from agentflow.workspace.cmdb import MockCmdbProvider  # noqa: E402

# SCENARIOS §3.2 场景2 bug report（结账无响应，无报错 → 挂起类）
BUG_REPORT = {
    "number": "INC0012346",
    "short_description": "订单服务结账无响应",
    "description": "业务人员结账时页面长时间无响应、一直转圈，无任何报错提示。影响全部结账业务，已持续 15 分钟。",
    "category": "Application",
    "subcategory": "order-service",
    "impact": "1 - High",
    "urgency": "1 - High",
    "priority": "P1",
    "state": "New",
    "cmdb_ci": {"name": "order-service", "service": "订单服务", "namespace": "order"},
    "symptom": "结账请求挂起 / 无响应（无报错）",
}

# 诊断链（场景2：trace 是胜负手，metrics/infra 为负证据排除）
AGENTS = ["triage", "log-analyst", "trace-analyst", "metrics-analyst", "infra-locator",
          "code-locator", "knowledge-lookup", "root-cause"]


async def main() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("缺 DEEPSEEK_API_KEY（可 source spike/.env）")
    ds = RealDataSourceAdapter()
    cmdb = MockCmdbProvider()  # team-alpha: order-service + warranty-service → repo
    model = build_model()
    print(f"模型: {model.model} | 数据源: ES/Prometheus/kubectl | CMDB: mock\n")

    evidence: dict[str, str] = {}
    for name in AGENTS:
        toolkit = build_toolkit(name, use_mock=True, datasource=ds, cmdb=cmdb)
        # trace-analyst 需要更多迭代（2 个工具 + 链合成）；其余用默认
        agent = build_agent(name, toolkit, model, max_iters=12 if name == "trace-analyst" else 10)
        user_input = {"bug": BUG_REPORT} if name == "triage" else {"bug": BUG_REPORT, "evidence": evidence}
        out = await run_agent(agent, user_input)
        evidence[name] = out
        print(f"[{name}]")
        print("  ", json.dumps(out, ensure_ascii=False))
        print()

    rca = evidence.get("root-cause", {})
    rca_type = rca.get("root_cause_type")
    conf = rca.get("confidence")
    hypotheses = rca.get("hypotheses", [])
    print("=" * 50)
    print(f"根因类型: {rca_type}  置信度: {conf}")
    print("候选根因:", json.dumps(hypotheses, ensure_ascii=False))
    if rca_type == "code_bug":
        print("✅ 场景2 期望根因 code_bug（warranty-service 代码缺陷）—— 命中")
    else:
        print(f"⚠️ 根因类型为 {rca_type}，与场景2 期望（code_bug）不符")
    await ds.aclose()


if __name__ == "__main__":
    asyncio.run(main())
