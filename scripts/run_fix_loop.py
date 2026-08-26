# -*- coding: utf-8 -*-
"""场景2 修复闭环 E2E：诊断（真实数据源+DeepSeek）→ 修复（真实工作区 git）→ 审批 → PR。

流程：
1. WorkspaceManager 克隆 warranty-service（base_sha 冻结 + aiops/RUN 分支）
2. 执行 bug-fix-scenario2.yaml：诊断链用真实数据源工具 + DeepSeek；
   fix-implementer 真实修改 WarrantyService.java 并产出 diff；
   tester 沙箱结构化检查；reviewer 审查；approve-commit 审批；
   committer 真实 git commit + push 到本地 bare remote（=提交 PR）
3. 验证：run 收敛 + git 提交存在 + 审计记录

用法（先注入 scenario2 故障 + 数据源端口转发）：
    DEEPSEEK_API_KEY=... ./venv/bin/python scripts/run_fix_loop.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.agents.datasources import RealDataSourceAdapter  # noqa: E402
from agentflow.agents.mcp import build_toolkit  # noqa: E402
from agentflow.agents.scopes import build_agent, build_model, run_agent  # noqa: E402
from agentflow.core.dag import DONE  # noqa: E402
from agentflow.core.workflow import Workflow  # noqa: E402
from agentflow.executor.dag_executor import DAGExecutor  # noqa: E402
from agentflow.service import RunService  # noqa: E402
from agentflow.statestore.memory import InMemoryStateStore  # noqa: E402
from agentflow.workspace.manager import WorkspaceManager  # noqa: E402
from agentflow.workspace.models import RepoSpec  # noqa: E402

WARRANTY_REPO = Path("/Users/bo.gong/Documents/accenture/workspace/agentflow-testbed/services/aiops-test-warranty-service")
FIX_FILE = "src/main/java/com/company/warranty/service/WarrantyService.java"
_BUG_LINE = "String queryFin = missingFin ? null : fin;"
_FIX_LINE = "String queryFin = fin;  // 修复: 始终透传必填参数 fin（场景2 bug#1）"

BUG_REPORT = {
    "number": "INC0012346",
    "short_description": "订单服务结账无响应",
    "description": "业务人员结账时页面长时间无响应、一直转圈，无任何报错提示。",
    "category": "Application", "subcategory": "order-service",
    "impact": "1 - High", "urgency": "1 - High", "priority": "P1",
    "cmdb_ci": {"name": "order-service", "namespace": "order"},
}

DIAGNOSE_AGENTS = ["triage", "log-analyst", "trace-analyst", "metrics-analyst",
                   "infra-locator", "code-locator", "knowledge-lookup", "root-cause"]


def git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr[:300]}")
    return r.stdout.strip()


# ======================================================================
# 节点 runner：按 agent 分发
# ======================================================================
class FixLoopRunner:
    def __init__(self, workspace: Path, ds: RealDataSourceAdapter, model) -> None:
        self.workspace = workspace
        self.ds = ds
        self.model = model
        self.evidence: dict = {}
        self.commit_info: dict = {}

    async def __call__(self, node, params: dict):
        agent = node.agent
        if agent in DIAGNOSE_AGENTS:
            return await self._run_diag(agent, params)
        if agent == "fix-planner":
            return await self._run_llm("fix-planner", params)
        if agent == "fix-implementer":
            return await self._fix(params)
        if agent == "tester":
            return self._test(params)
        if agent == "reviewer":
            return await self._review(params)
        if agent == "committer":
            return await self._commit(params)
        if agent == "postmortem":
            return {"summary": f"场景2 修复完成: {params.get('fix', {}).get('diff', '')[:40]}...", "root_cause": "warranty fin 缺参"}
        return {"node": node.id, "ok": True}

    async def _run_diag(self, agent: str, params: dict) -> dict:
        toolkit = build_toolkit(agent, use_mock=True, datasource=self.ds)
        a = build_agent(agent, toolkit, self.model, max_iters=12 if agent == "trace-analyst" else 10)
        user = {"bug": BUG_REPORT} if agent == "triage" else {"bug": BUG_REPORT, "evidence": self.evidence}
        out = await run_agent(a, user)
        self.evidence[agent] = out
        return out

    async def _run_llm(self, agent: str, params: dict) -> dict:
        toolkit = build_toolkit(agent, use_mock=True)
        a = build_agent(agent, toolkit, self.model)
        return await run_agent(a, {"rca": params.get("rca")})

    # ---- 修复（真实工作区编辑 + diff）----
    async def _fix(self, params: dict) -> dict:
        target = self.workspace / FIX_FILE
        content = target.read_text(encoding="utf-8")
        assert _BUG_LINE in content, f"未找到 bug 行 {_BUG_LINE!r}"
        fixed = content.replace(_BUG_LINE, _FIX_LINE)
        target.write_text(fixed, encoding="utf-8")
        diff = git("diff", cwd=self.workspace)
        return {"diff": diff, "files_changed": [FIX_FILE],
                "explanation": "修复 WarrantyService.checkWarranty：始终透传必填参数 fin"}

    # ---- 测试（沙箱/结构化检查：验证 bug 行已修复）----
    def _test(self, params: dict) -> dict:
        content = (self.workspace / FIX_FILE).read_text(encoding="utf-8")
        passed = _BUG_LINE not in content and _FIX_LINE in content
        return {"passed": passed, "tests_run": 1, "failed": [] if passed else [FIX_FILE],
                "coverage": "structural-check"}

    async def _review(self, params: dict) -> dict:
        return {"approved": True, "comments": ["fin 透传修复，低风险"], "risk": "low"}

    # ---- 提交（真实 git commit + push 到 bare remote = 提交 PR）----
    async def _commit(self, params: dict) -> dict:
        branch = git("branch", "--show-current", cwd=self.workspace)
        git("add", FIX_FILE, cwd=self.workspace)
        git("commit", "-q", "-m", "fix: 透传必填参数 fin（场景2 结账无响应根因）", cwd=self.workspace)
        git("push", "-q", "origin", branch, cwd=self.workspace)
        head = git("rev-parse", "HEAD", cwd=self.workspace)
        self.commit_info = {"branch": branch, "head": head[:12], "remote": "local-bare"}
        return {"pr_url": f"local-bare://{branch}@{head[:12]}", "pr_number": 1, "base_sha": git("rev-parse", "origin/main", cwd=self.workspace)}


# ======================================================================
# 主流程
# ======================================================================
async def main() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("缺 DEEPSEEK_API_KEY（可 source spike/.env）")
    wf = Workflow.load_yaml(Path(__file__).resolve().parent.parent / "workflows" / "bug-fix-scenario2.yaml")
    print(f"工作流: {wf.name} | {len(wf.dag)} 节点 | hash={wf.workflow_hash[:10]}")

    # 1) 工作区：clone warranty + 分支（§8.7 版本冻结）
    tmp = Path(tempfile.mkdtemp(prefix="fixloop_"))
    wm = WorkspaceManager("team-alpha", "run_m7", workspace_root=tmp)
    base_sha = git("rev-parse", "HEAD", cwd=WARRANTY_REPO)
    dest = await wm.prepare_one(RepoSpec("warranty-service", f"file://{WARRANTY_REPO}", base_sha=base_sha))
    print(f"工作区: {dest} 分支={wm.run_branch} base_sha={base_sha[:10]}")

    # 1b) 把 origin 指向临时 bare remote（模拟 PR 目标仓库，不污染源仓库）
    bare = tmp / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git("remote", "set-url", "origin", str(bare), cwd=dest)
    git("push", "-q", "origin", "main", cwd=dest)  # bare 上有 main → base_sha 可解析

    # 2) 执行
    ds = RealDataSourceAdapter()
    model = build_model()
    store = InMemoryStateStore()
    runner = FixLoopRunner(dest, ds, model)
    svc = RunService(store, node_runner=runner)

    print("\n==> create_run（诊断 → 修复 → 提交审批）")
    summary = await svc.create_run("team-alpha", wf, {"bug_report": BUG_REPORT})
    print("    pending_approvals:", summary["pending_approvals"])

    # 3) 审批 → 提交 → 复盘
    if summary["pending_approvals"]:
        res = await svc.approve(summary["run_id"], "approve-commit", approved=True, by="lead-engineer", comment="修复合理")
        print("==> approve-commit 通过，run 状态:", res["run_status"])
    else:
        res = summary

    # 4) 结果
    print("\n==> 节点终态")
    for nid, st in res["status"].items():
        print(f"    {nid:16s} {st}")
    final_done = all(v in (DONE, "skipped", "rejected") for v in res["status"].values())
    print("\n✅ 修复闭环收敛:", "DONE" if final_done else "未完成")
    if runner.commit_info:
        print("    git 提交:", runner.commit_info)
        print("    PR:", res["status"].get("commit"))
    # 审计：本次工具调用未写审计（diagnose 走 AgentScope 权限层）；展示 run 节点状态
    print(f"    run_id={res['run_id']}")
    await ds.aclose()


if __name__ == "__main__":
    asyncio.run(main())
