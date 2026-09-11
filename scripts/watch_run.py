"""观察一次 run 的逐阶段推进（无 UI 时的命令行观测面）。

用法：
    ./venv/bin/python scripts/watch_run.py <run_id>
    ./venv/bin/python scripts/watch_run.py --recent              # 取最近一次 run
    ./venv/bin/python scripts/watch_run.py <run_id> --traces     # 附每节点 LLM/工具明细
    ./venv/bin/python scripts/watch_run.py <run_id> --once       # 只打印一次快照
    ./venv/bin/python scripts/watch_run.py --recent --auto-approve approve-commit

观测面（对齐 design §6 / v5.3）：
    1. ``GET /runs/{id}``        → 节点状态 / 输出 / token / cost / 待审批
    2. ``GET /runs/{id}/traces`` → 每次 LLM 调用的 messages、工具调用、被 DENY 的工具
    3. 直查库取 run_id（无 ``GET /runs`` 列表端点，见 design-v5.6 §4.7.2）

仅依赖标准库 + 本仓 config，不引入新依赖。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 终态（不再轮询）；queued/running/waiting_approval/paused 都算"还在推进"
_TERMINAL_RUN = {"success", "failed", "cancelled"}
_NODE_DONE = {"done", "skipped", "rejected", "rejected-canceled"}

_COLOR = {
    "done": "\033[32m",                 # 绿
    "running": "\033[33m",              # 黄
    "waiting_approval": "\033[35m",     # 紫
    "rejected": "\033[31m",
    "rejected-canceled": "\033[31m",
    "failed": "\033[31m",
    "skipped": "\033[90m",
    "pending": "\033[90m",
}
_RESET, _BOLD, _DIM = "\033[0m", "\033[1m", "\033[2m"


def _c(text: str, status: str) -> str:
    return f"{_COLOR.get(status, '')}{text}{_RESET}"


# ======================================================================
# HTTP
# ======================================================================
def _get(base: str, path: str, params: dict | None = None, tenant: str = "local") -> Any:
    url = f"{base.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Tenant-ID": tenant})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(base: str, path: str, body: dict, tenant: str = "local") -> Any:
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Tenant-ID": tenant},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ======================================================================
# run_id 解析：--recent 直查库
# ======================================================================
def _recent_run_id(tenant: str) -> str:
    from agentflow.config import get_settings, postgres_dsn

    s = get_settings()
    if s.state_store == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(postgres_dsn(s), row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT run_id, status, created_at FROM runs WHERE tenant_id=%s"
                " ORDER BY created_at DESC LIMIT 1",
                (tenant,),
            ).fetchone()
        if row is None:
            raise SystemExit(f"库中找不到租户 {tenant} 的 run（先 POST /run 触发一次）")
        return row["run_id"]

    import sqlite3

    for db in (s.state_db_path, Path(s.state_db_path).parent / "tenants" / f"{tenant}.db"):
        if not Path(db).exists():
            continue
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE tenant_id=? ORDER BY created_at DESC LIMIT 1",
                (tenant,),
            ).fetchone()
            if row:
                return row[0]
    raise SystemExit(f"库中找不到租户 {tenant} 的 run（先 POST /run 触发一次）")


# ======================================================================
# 渲染
# ======================================================================
def _brief(value: Any, limit: int = 150) -> str:
    """输出单行摘要（dict 优先取常见摘要字段，超长截断）。"""
    if value is None:
        return "—"
    if isinstance(value, dict):
        for key in ("summary", "root_cause_type", "error_message", "explanation", "status"):
            if key in value and value[key] not in (None, ""):
                return str(value[key])[:limit]
        return json.dumps(value, ensure_ascii=False)[:limit]
    return str(value)[:limit]


def _agent_of(run: dict, nid: str) -> str:
    for n in run.get("graph", {}).get("nodes", []):
        if n["id"] == nid:
            return n.get("agent") or n.get("kind") or ""
    return ""


def _print_nodes(run: dict, order: list[str], indent: str = "  ") -> None:
    nodes = run.get("nodes", {})
    for nid in order:
        cp = nodes.get(nid, {})
        st = str(cp.get("status"))
        tk = cp.get("tokens") or 0
        extra = f"  {_DIM}[{tk} tok]{_RESET}" if tk else ""
        print(f"{indent}{nid:<18} {_c(f'{st:<18}', st)}{_agent_of(run, nid):<22}{extra}")
        out = cp.get("output")
        if out is not None:
            print(f"{indent}  ⤷ {_DIM}{_brief(out)}{_RESET}")


def _print_pending(run: dict, base: str, tenant: str) -> None:
    pend = run.get("pending_approvals") or []
    if not pend:
        return
    print(f"\n{_BOLD}⏸  待人工审批（run 已挂起，executor 零占用）{_RESET}")
    for p in pend:
        nid = p["node_id"]
        print(f"   节点: {_c(nid, 'waiting_approval')}")
        for up, out in (p.get("upstream") or {}).items():
            print(f"     ← {up}: {_brief(out, 110)}")
        print(f"     {_DIM}放行:{_RESET} curl -s -X POST {base}/runs/{run['run_id']}/approve \\")
        print("            -H 'Content-Type: application/json' -H 'X-Tenant-ID: "
              f"{tenant}' \\")
        print(f"            -d '{{\"node_id\":\"{nid}\",\"approved\":true,\"by\":\"lead-engineer\"}}'")


def _print_traces(base: str, run_id: str, tenant: str) -> None:
    """每节点 LLM/工具明细；DENY 单独告警（DONT_ASK 下无 allow 即全 DENY）。"""
    try:
        rows = _get(base, f"/runs/{run_id}/traces", tenant=tenant)
    except urllib.error.HTTPError as exc:
        print(f"  (traces 读取失败: HTTP {exc.code})")
        return
    if not rows:
        print("\n(无 traces：mock runner 不产明细；真实 runner 才有)")
        return
    by_node: dict[str, dict[str, int]] = {}
    denied: list[dict] = []
    for r in rows:
        slot = by_node.setdefault(r["node_id"], {})
        slot[r["kind"]] = slot.get(r["kind"], 0) + 1
        if r["kind"] == "denied":
            denied.append(r)
    print(f"\n{_BOLD}节点明细（llm 调用 / 工具调用 / 被拒）{_RESET}")
    for nid, s in by_node.items():
        d = s.get("denied", 0)
        print(f"  {nid:<18} llm={s.get('llm_call', 0):<3} tool={s.get('tool_call', 0):<3} "
              f"denied={_c(str(d), 'failed' if d else 'done')}")
    if denied:
        print(f"\n{_c('⚠ 有工具被权限拒绝（DONT_ASK 下无 allow 规则 = 全 DENY）', 'failed')}")
        for d in denied[:5]:
            print(f"   {d['node_id']}: {json.dumps(d.get('payload'), ensure_ascii=False)[:150]}")


# ======================================================================
# 主轮询
# ======================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="观察 agentflow run 的逐阶段推进")
    ap.add_argument("run_id", nargs="?", help="run id（省略则用 --recent）")
    ap.add_argument("--api", default="http://localhost:8000", help="控制面地址")
    ap.add_argument("--tenant", default="local", help="租户（dev 模式经 X-Tenant-ID 头）")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--timeout", type=float, default=1800.0, help="最长观察秒数")
    ap.add_argument("--recent", action="store_true", help="取最近一次 run（直查库）")
    ap.add_argument("--once", action="store_true", help="只打印一次快照")
    ap.add_argument("--traces", action="store_true", help="结束时附节点明细")
    ap.add_argument("--auto-approve", metavar="NODE", action="append", default=[],
                    help="自动放行指定审批节点（可多次；仅用于无人值守 E2E）")
    args = ap.parse_args()

    run_id = args.run_id or (_recent_run_id(args.tenant) if args.recent else None)
    if not run_id:
        raise SystemExit("需要 run_id 或 --recent")

    print(f"观察 {run_id} @ {args.api}（Ctrl-C 退出）\n")
    completed: set[str] = set()
    deadline, last_status, announced_pending = time.time() + args.timeout, None, set()

    while True:
        try:
            run = _get(args.api, f"/runs/{run_id}", tenant=args.tenant)
        except urllib.error.HTTPError as exc:
            raise SystemExit(
                f"读取 run 失败: HTTP {exc.code}（租户 {args.tenant} 可能不匹配）"
            ) from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"连不上控制面 {args.api}：{exc.reason}（先起 API）") from exc

        if last_status != run["status"]:
            if last_status is not None:
                print(f"\n{_BOLD}── 状态流转: {last_status} → "
                      f"{_c(run['status'], run['status'])} ──{_RESET}")
            last_status = run["status"]

        nodes = run.get("nodes", {})
        order = [n["id"] for n in run.get("graph", {}).get("nodes", [])]
        fresh = [nid for nid in order
                 if nid not in completed and nodes.get(nid, {}).get("status") in _NODE_DONE]
        if fresh:
            print(f"{_BOLD}[新完成]{_RESET}")
            _print_nodes(run, fresh)
            completed.update(fresh)

        for nid in args.auto_approve:
            if nodes.get(nid, {}).get("status") == "waiting_approval":
                try:
                    _post(args.api, f"/runs/{run_id}/approve",
                          {"node_id": nid, "approved": True, "by": "lead-engineer",
                           "comment": "watch_run 自动放行"}, args.tenant)
                    print(f"\n▶ 已自动放行 {_c(nid, 'done')}")
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", "replace")[:200]
                    print(f"\n⚠ 自动放行 {nid} 失败: HTTP {exc.code} {body}")

        if run["status"] in _TERMINAL_RUN or args.once:
            break
        if time.time() > deadline:
            print(f"\n⚠ 观察超时（{args.timeout}s），run 仍在 {run['status']}")
            break

        pend = run.get("pending_approvals") or []
        if pend:
            new = {p["node_id"] for p in pend} - announced_pending
            if new:
                _print_pending(run, args.api, args.tenant)
                announced_pending |= new
            print(f"{_DIM}（等待人工审批，每 {args.interval}s 复查…）{_RESET}")
        time.sleep(args.interval)

    # ── 收尾：全量快照 + 汇总 ──
    run = _get(args.api, f"/runs/{run_id}", tenant=args.tenant)
    ok = run["status"] == "success"
    print(f"\n{_BOLD}══ 最终状态 ══{_RESET}  "
          f"{_c(str(run['status']), 'done' if ok else 'failed')}")
    _print_nodes(run, [n["id"] for n in run.get("graph", {}).get("nodes", [])])
    print(f"\ntoken={run.get('total_tokens', 0)}  cost=${run.get('total_cost', 0.0):.6f}")
    if run.get("pending_approvals"):
        _print_pending(run, args.api, args.tenant)
    if args.traces:
        _print_traces(args.api, run_id, args.tenant)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n(已退出观察；run 仍在后台推进)")
