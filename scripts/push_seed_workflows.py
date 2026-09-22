#!/usr/bin/env python3
"""把 `agentflow/seed/workflows/` 里的**全部** workflow 推到某个已开通租户。

    ./venv/bin/python scripts/push_seed_workflows.py --tenant otr
    ./venv/bin/python scripts/push_seed_workflows.py --tenant otr --dry-run
    make sync-workflows TENANT=otr

## 为什么需要它

**workflow 的真源是数据库，不是仓库里的 YAML**（CLAUDE.md §6.0）。而 seed 的语义是
「**空表才播、绝不覆盖**」—— 所以改了 `seed/workflows/*.yaml`：

- **对新租户**：建库时自动生效（`TenantStoresRouter._build()` 会播种）。
- **对已开通租户**：**完全没有任何效果，也没有任何提示**。实测踩过：改完 YAML、run 跑的还是旧流程。

第二条路此前**没有工具**：仓库里只有 `seed_problem_log_diagnose.py`，而它只服务
`problem-log-diagnose` **一条**流程。于是"把改动同步到别人机器"只能手搓 curl ——
而手搓的失败方式是**静默的**：漏推一条不会报错，只会在很久以后表现为"这条流程的行为
和 YAML 对不上"。

## 它做什么

1. 逐条 `POST /workflows/preview`（**先验后存**：静态校验不过就不推）
2. 按 **name** 在目标租户里找同名的：
   - 命中 → `PUT`（**保 id** —— 前端缓存的 workflow id 不失效，且 `created_at` 不变）
   - 未命中 → `POST`（⚠️ 会拿到**新的** `created_at`，见下面的告警）
3. **推完回读校验**：把库里那份 YAML 与文件**逐字节**比对，不一致就报错退出

## ⚠️ 它**不**覆盖什么（别以为跑完就全同步了）

**数据面**：MCP server 注册（`seed/dataplane.yaml`）、agent↔server 绑定、
自定义 agent 定义（`seed/agents/*.yaml`，如 `ticket-done`）—— 那是
`scripts/push_seed_agents.py`（`make sync-agents TENANT=…`）的活。

两边都只在**空表**时播种，改了对已开通租户同样无效；**两条命令合起来**才是完整的
「把 seed 推到已开通租户」。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.seed import load_workflow_seeds  # noqa: E402


class Api:
    def __init__(self, base: str, tenant: str, dry_run: bool) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.dry_run = dry_run

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict | list:
        if self.dry_run and method != "GET":
            print(f"      [dry-run] {method} {path}")
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
            detail = exc.read().decode(errors="replace")[:400]
            raise SystemExit(f"✗ {method} {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"✗ {method} {path} → 连不上 {self.base}（{exc.reason}）") from exc


def push_one(api: Api, item: dict, existing_id: str | None) -> str:
    """推一条 workflow，返回它的 id（dry-run 时返回空）。"""
    name, yaml_text = item["name"], item["yaml"]
    # 先验后存：preview 会跑 workflow 的静态校验（环 / join / 挂了 / params 引用），
    # 不过就不推 —— 推一份坏图上去，症状要到某条 run 跑到那个节点才出现。
    api("POST", "/workflows/preview", {"yaml": yaml_text})

    if existing_id:
        # ⚠️ **不传 name**：`PUT /workflows/{wid}` 的契约是「`name` 缺失 ⇒ 保留库里原值」。
        # 传了就会用 seed 里的 name 覆盖 —— 那通常一样，但**不一样时不该由这个脚本决定**。
        api("PUT", f"/workflows/{existing_id}", {"yaml": yaml_text})
        print(f"    ✓ PUT（保 id {existing_id}，created_at 不变）")
        return existing_id

    created = api("POST", "/workflows", {"name": name, "yaml": yaml_text})
    print(f"    ✓ POST（新建 id={created.get('id') if isinstance(created, dict) else '?'}）")
    print(
        "      ⚠️ 新建 ⇒ 它的 created_at 是**现在**，于是成了该租户**最新**的一条。\n"
        "         未钉 workflow 的工单（手工单）兜底跑的就是 `saved[0]` —— 这条会成为新的兜底。\n"
        "         要维持原来的兜底，去 workflow 页把它排回去，或给手工单钉一条。"
    )
    return str(created.get("id", "")) if isinstance(created, dict) else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="把 seed 里的 workflow 推到已开通租户")
    ap.add_argument("--tenant", required=True, help="目标租户 id（如 otr）")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="agentflow 控制面地址")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不发写请求")
    args = ap.parse_args()

    api = Api(args.base, args.tenant, args.dry_run)
    seeds = load_workflow_seeds()
    if not seeds:
        raise SystemExit("✗ 读不到任何 seed workflow（agentflow/seed/workflows/）")

    print(f"== 把 {len(seeds)} 条 seed workflow 推到租户 {args.tenant} ==")
    existing = {w.get("name"): w.get("id") for w in api("GET", "/workflows")}

    pushed: dict[str, str] = {}
    for item in seeds:
        wid = existing.get(item["name"])
        print(f"\n  {item['name']}  ({'更新' if wid else '新建'})")
        pushed[item["name"]] = push_one(api, item, wid)

    if args.dry_run:
        print("\n（dry-run：没有发任何写请求）")
        return 0

    # ── 推完回读校验 ──────────────────────────────────────────────
    #
    # **不做这一步的话，失败是静默的**：PUT 返回 200 不等于库里那份就是你刚推的
    # （写别的租户、写成功但被后续覆盖、YAML 里有会被解析器规整掉的东西……）。
    # 逐字节比对是最便宜的判据，且不依赖任何一方的自述。
    print("\n== 回读校验（库里那份 vs 文件，逐字节） ==")
    bad: list[str] = []
    for item in seeds:
        wid = pushed.get(item["name"])
        if not wid:
            bad.append(f"{item['name']}: 没拿到 id")
            continue
        got = api("GET", f"/workflows/{wid}")
        stored = got.get("yaml") if isinstance(got, dict) else None
        if stored != item["yaml"]:
            bad.append(f"{item['name']}: 库里的 YAML 与文件**不一致**")
            print(f"  ✗ {item['name']}")
        else:
            print(f"  ✓ {item['name']}")

    if bad:
        print("\n✗ 以下条目没推成功：")
        for b in bad:
            print(f"    {b}")
        return 1

    names = [i["name"] for i in seeds]
    print(
        f"\n✓ {len(seeds)} 条全部就位：{', '.join(names)}\n"
        "\n⚠️ 本脚本**只同步 workflow**。数据面（MCP server 注册 + agent 绑定 +"
        "\n   自定义 agent）走：\n"
        f"     make sync-agents TENANT={args.tenant}\n"
        "   两条合起来才是完整的「把 seed 推到已开通租户」。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
