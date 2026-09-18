#!/usr/bin/env python3
"""把「Problem Center 分析new」的种子幂等推到某个 agentflow 租户。

内容 = workflow（`problem-log-diagnose`）+ 数据面 MCP server 注册 + agent 绑定。

⚠️ **真源是数据库，不是仓库文件**（CLAUDE.md §6.0 / docs/TODO.md §13）：
   本脚本就是 TODO §13 里缺的那条「同步动作」——一条命令把种子推到目标租户，
   而不是手工 `POST /workflows` / `PUT /agent-configs`。
   同目录的 `problem-log-diagnose.workflow.yaml` 只是**推送载荷**，不参与运行时；
   改了它必须重跑本脚本，否则 run 跑的还是库里那份旧流程（且没有任何提示）。

幂等：按 name 命中 → PUT（保 id，前端缓存的 workflow id 不失效）；未命中 → POST。
只调 HTTP API，不碰数据库（不破坏分层约束 §11）。

用法：
    ./venv/bin/python scripts/seed_problem_log_diagnose.py --tenant otr
    ./venv/bin/python scripts/seed_problem_log_diagnose.py --tenant otr --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKFLOW_YAML = HERE / "problem-log-diagnose.workflow.yaml"
WORKFLOW_NAME = "problem-log-diagnose"
MCP_NAME = "aiops-datasource"
# 只绑**真的要用 MCP 工具**的 agent（见 CLAUDE.md §7 数据面姿态 / docs/TODO.md §16）：
#   log-analyst  → query_logs / get_trace        （日志是本流程唯一证据源）
#   code-locator → locate_repo / get_service_topology（其提示词点名要这两个工具）
# 不绑：triage（明确无工具）、knowledge-lookup（本地 search_knowledge）、
#       fix-planner/postmortem（纯推理）、root-cause（绑了会自行查指标/基础设施，打破 log-only 边界）
BOUND_AGENTS = ["log-analyst", "code-locator"]


class Api:
    def __init__(self, base: str, tenant: str, dry_run: bool) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.dry_run = dry_run

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.dry_run and method != "GET":
            print(f"    [dry-run] {method} {path} {json.dumps(body, ensure_ascii=False)[:120]}")
            return {}
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Tenant-ID": self.tenant},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise SystemExit(f"✗ {method} {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"✗ {method} {path} → 连不上 {self.base}（{exc.reason}）") from exc


def seed_workflow(api: Api) -> str:
    yaml_text = WORKFLOW_YAML.read_text(encoding="utf-8")
    # 先验后存：preview 会跑 workfow 的静态校验（环 / join / params 引用），失败即退出
    api("POST", "/workflows/preview", {"yaml": yaml_text})

    existing = next(
        (w for w in api("GET", "/workflows") if w.get("name") == WORKFLOW_NAME), None
    )
    if existing:
        api("PUT", f"/workflows/{existing['id']}", {"name": WORKFLOW_NAME, "yaml": yaml_text})
        print(f"  ✓ workflow {WORKFLOW_NAME} 已更新（PUT，保 id）id={existing['id']}")
        return existing["id"]
    created = api("POST", "/workflows", {"name": WORKFLOW_NAME, "yaml": yaml_text})
    print(f"  ✓ workflow {WORKFLOW_NAME} 已创建 id={created.get('id')}")
    return created.get("id", "")


def seed_mcp(api: Api, mcp_url: str) -> str:
    servers = api("GET", "/mcp-servers")
    existing = next((s for s in servers if s.get("name") == MCP_NAME), None)
    body = {"name": MCP_NAME, "transport": "http", "config": {"url": mcp_url}, "enabled": True}
    if existing:
        # tools 不传 → 服务端保留已有快照（不重新 fetch、不覆盖）
        api("PUT", f"/mcp-servers/{existing['id']}", body)
        print(f"  ✓ MCP server {MCP_NAME} 已更新 id={existing['id']} url={mcp_url}")
        return existing["id"]
    created = api("POST", "/mcp-servers", body)
    mid = created.get("id", "")
    tools = created.get("tools") or []
    print(f"  ✓ MCP server {MCP_NAME} 已注册 id={mid} url={mcp_url} tools={len(tools)}")
    return mid


def seed_binding(api: Api, agent: str, mid: str) -> None:
    row = api("GET", f"/agent-configs/{agent}")
    current = row.get("mcp_server_ids") or []
    if mid in current:
        print(f"  = agent {agent} 已绑定（跳过）servers={current}")
        return
    stored = row.get("stored") or {}
    # ⚠️ PUT 是**完整对象覆盖**：description/system_prompt/output_schema 只认请求体，
    #    不传就归 NULL（回退内置）——即"给 agent 加一个 server 绑定"会顺手抹掉别人
    #    在页面上定制过的提示词。故这里把库里已存文本**原样回传**。
    api("PUT", f"/agent-configs/{agent}", {
        "description": stored.get("description"),
        "system_prompt": stored.get("system_prompt"),
        "output_schema": stored.get("schema"),
        "mcp_server_ids": [*current, mid],
    })
    print(f"  ✓ agent {agent} 绑定 {MCP_NAME} servers={[*current, mid]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="推送 problem-log-diagnose 种子（幂等）")
    ap.add_argument("--tenant", default="otr", help="agentflow 租户（X-Tenant-ID），默认 otr")
    ap.add_argument("--api", default="http://localhost:8000", help="agentflow 基址")
    ap.add_argument("--mcp-url", default="http://127.0.0.1:8300/mcp", help="数据面 MCP server 地址")
    ap.add_argument("--skip-mcp", action="store_true", help="只推 workflow，不注册/绑定数据面")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发起的写请求")
    args = ap.parse_args(argv)

    print(
        "本脚本是**推送工具**：workflow 的真源是数据库，"
        f"{WORKFLOW_YAML.name} 只是载荷，不参与运行时。"
    )
    print(f"目标：{args.api}  租户：{args.tenant}{'  [dry-run]' if args.dry_run else ''}")
    api = Api(args.api, args.tenant, args.dry_run)

    wid = seed_workflow(api)
    mid = ""
    if not args.skip_mcp:
        mid = seed_mcp(api, args.mcp_url)
        for agent in BOUND_AGENTS:
            seed_binding(api, agent, mid)

    print("\n完成。下一步：")
    print(f"  workflow_id = {wid}   （前端「分析new」按 name 解析，通常无需填）")
    if mid:
        print(f"  mcp_server_id = {mid}")
    print(f"  核对：curl -s -H 'X-Tenant-ID: {args.tenant}' {args.api}/workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
