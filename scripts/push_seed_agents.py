#!/usr/bin/env python3
"""把 seed 的**数据面**推到某个已开通租户：MCP server 注册 + agent 绑定 + 自定义 agent。

    ./venv/bin/python scripts/push_seed_agents.py --tenant otr
    ./venv/bin/python scripts/push_seed_agents.py --tenant otr --dry-run
    make sync-agents TENANT=otr

## 为什么需要它

seed 的语义是「**空表才播、绝不覆盖**」（`agentflow/seed/__init__.py`）—— 所以
`git pull` 对**已开通租户毫无效果、也没有任何提示**。workflow 那一半已经由
`make sync-workflows` 补上了；**数据面这一半此前没有工具**，只能手搓 curl。

实测代价（2026-09-22，`run_62f21fa82f`）：租户 `otr` 的 `agent_configs` 少了 4 行
（`service-scoper` / `trace-analyst` / `metrics-analyst` / `infra-locator`）。
少了行 ⇒ `mcp_server_ids` 为 NULL ⇒ `AgentConfigResolver.server_ids_for()` 返回空集
（v1.12.1 两态语义：**没配置就没有 server**）⇒ 该 agent **零工具**。而
`service-scoper` 的内置提示词硬点名要调 MCP 工具 `infer_candidate_services` ——
没有工具可调，模型把这次调用写成了**纯文本**，`extract_json` 解不出 JSON ⇒
`AgentOutputError` ⇒ `scope` 节点失败 ⇒ `on_failure: abort` **中止整条 run**。

症状是"某个 agent 不会用工具"，成因是**一行绑定缺失**，中间没有任何一步会报
"绑定缺失"—— 这正是本脚本要挡的那类。判据一句话：**agent 的工具来自绑定，
而绑定不在代码里。**

## 它做什么

1. MCP server 按 **name** 对齐：种子声明了、租户里没有 → 注册（URL 从配置取，
   **并打印出来**供核对环境）；已有 → **不动**
2. 内置 agent 的绑定按 **name** 对齐：**并集，只加不删**
   （租户自己加的 server 会被保留 —— 实测 `otr` 的 `code-locator` 多绑了一个 `git-server`）
3. 自定义 agent（`seed/agents/*.yaml`）：**缺行就建**（带 prompt / schema / 绑定）；
   **已有行不动**（prompt 是本地可改的），但缺绑定会告警
4. **推完回读校验**（`GET /agents`）：期望集必须是读回集的**子集**，否则非零退出

## ⚠️ 两条边界（别以为跑完就全同步了）

- **只同步数据面**。workflow 走 `make sync-workflows` —— 两条合起来才是完整的"推 seed"。
- **永不删除**：本脚本不会移除租户已有的任何绑定。种子将来若"故意解绑"某个 server，
  这条路推不下去（要另做 `--prune` 之类，本次不做）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.agents.registry import DIAGNOSE_AGENTS, FIX_AGENTS  # noqa: E402
from agentflow.config import get_settings  # noqa: E402
from agentflow.seed import load_custom_agent_seeds, load_dataplane_seed  # noqa: E402

_BUILTIN = set(DIAGNOSE_AGENTS) | set(FIX_AGENTS)

#: 自定义文本三件套在推前快照里的"行没了"哨兵值（与 (None, None, None) 不相等）。
_MISSING_TEXT: tuple = ("<行不存在>",)


def _norm(t: tuple) -> tuple:
    """自定义文本的比较归一：`_acfg_str` 入库时会 strip，故两边都 strip 后再比。

    不归一的后果不是漏报而是**误报** —— "写了同样的值"会被判成改动，而误报和漏报
    一样会毁掉校验的可信度（人一旦开始忽略它，它就等于没有）。
    """
    return tuple(v.strip() if isinstance(v, str) else v for v in t)


class Api:
    def __init__(self, base: str, tenant: str, dry_run: bool) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.dry_run = dry_run

    def __call__(
        self, method: str, path: str, body: dict | None = None, *, allow_404: bool = False
    ) -> dict | list | None:
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
            # `allow_404` 只放行一种情形：`GET /agent-configs/{非内置名}` 在租户里还没有行。
            # 那是**预期内的分支**（本脚本的职责就是把它建出来），不该把整条命令杀掉；
            # 其余失败一律 fail-fast —— 它们没有"继续跑"的含义。
            if allow_404 and exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")[:400]
            raise SystemExit(f"✗ {method} {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"✗ {method} {path} → 连不上 {self.base}（{exc.reason}）") from exc


def _server_url(spec: dict) -> str | None:
    """seed 的 server 声明 → 真实 URL。

    与 `agentflow/seed/__init__.py:275-283` 同源：`url_setting` 指向配置项名，
    取不到就退回种子里的字面 `url`。**URL 是环境相关的**（本地 127.0.0.1、k8s 里是
    service DNS），所以它不该被硬编码，也不该在"server 已存在"时被本机的值覆盖。
    """
    setting_name = spec.get("url_setting")
    return getattr(get_settings(), setting_name, None) if setting_name else spec.get("url")


def sync_servers(api: Api, specs: list[dict]) -> dict[str, str]:
    """把种子声明的 MCP server 对齐到租户，返回 **server 名 → 真实 id** 映射。

    绑定在种子里写的是 **server 名**（`aiops-datasource`），而 `PUT /agent-configs/{name}`
    要的是 **server id** —— 且**同名 server 在不同租户里的 id 是不同的**
    （实测：`otr` 是 `754a5d7de62f`，`local` 是 `seed-aiops-datasource`）。
    所以必须按名读回，**不能假定 id 就是 `seed-<name>`** —— 绑到不存在的 id 上，
    症状同样是**静默零工具**（`agentflow/seed/__init__.py:291-294` 踩过一次）。
    """
    rows = {s["name"]: s for s in api("GET", "/mcp-servers")}
    ids = {name: s["id"] for name, s in rows.items()}
    for spec in specs:
        name = spec["name"]
        if name in ids:
            print(f"  = MCP server {name} 已存在（不动）id={ids[name]}")
            # 停用的 server **绑了等于没绑** —— 而绑定侧的判据全都会通过。
            # 只告警不改（那是运维的刻意选择，本脚本无权动 enabled）。
            if not rows[name].get("enabled", True):
                print("      ⚠️ 它的 enabled=false —— 绑上去也拿不到工具（本脚本不动它）")
            continue
        url = _server_url(spec)
        if not url:
            # 这不是"租户差异"，是**推的人自己的配置坏了**。判失败（见 main 里的
            # missing_servers）—— CLI 没有 seed 那条"跑在请求路径上、绝不能抛"的约束。
            print(f"  ✗ MCP server {name} 的地址取不到（url_setting={spec.get('url_setting')!r}）")
            continue
        body = {
            "name": name,
            "transport": spec.get("transport", "http"),
            "config": {"url": url},
            "is_stateful": bool(spec.get("is_stateful", 0)),
            "enabled": True,
        }
        created = api("POST", "/mcp-servers", body)
        # dry-run 下写请求被短路（返回 {}），拿不到真 id —— 给个占位符，别打印出
        # `servers=['']` 那种看着像 bug 的计划。
        mid = str(created.get("id", "")) if isinstance(created, dict) else ""
        ids[name] = mid or f"<将新建:{name}>"
        print(f"  ✓ MCP server {name} 已注册 id={ids[name]}")
        print(
            f"      ⚠️ URL 取自本机配置：{url}\n"
            "         地址是**环境相关**的 —— 它不是这个租户的固有属性。请核对：\n"
            "         这个地址从**该租户的 worker** 打得通吗？填错的症状是"
            "「agent 有工具、但工具全连不上」，与「绑定缺失」很像。"
        )
    return ids


def sync_binding(api: Api, agent: str, want: list[str]) -> tuple[list[str], bool]:
    """给一个内置 agent 补上缺失的绑定（**并集**）→ ``(应有的 id 集合, 是否发过写请求)``。

    第二个返回值决定打印 `✓` 还是 `=` —— 判据是**有没有真的写**，而不是
    "结果集等不等于 want"：租户多绑了 server（实测 `otr` 的 `code-locator` 多一个
    `git-server`）时 `got != want`，但它恰恰是**该跳过**的那种。

    `GET /agent-configs/{内置名}` 在租户没有覆盖行时返回**合成视图**
    （`mcp_server_ids: None`），不 404 —— 所以这里不需要容错分支。
    """
    row = api("GET", f"/agent-configs/{agent}")
    row = row if isinstance(row, dict) else {}
    current = list(row.get("mcp_server_ids") or [])
    missing = [i for i in want if i not in current]
    if not missing:
        return current, False

    stored = row.get("stored") or {}
    # ⚠️ `PUT /agent-configs/{name}` 对 description / system_prompt / output_schema /
    #    mcp_server_ids 是**无条件覆盖**，不传就写 NULL —— 即"只加一个 server 绑定"
    #    会顺手抹掉别人在页面上定制过的提示词。故把库里已存文本**原样回传**。
    #    （键名不对称：响应里叫 `schema`，请求字段叫 `output_schema`。）
    api("PUT", f"/agent-configs/{agent}", {
        "description": stored.get("description"),
        "system_prompt": stored.get("system_prompt"),
        "output_schema": stored.get("schema"),
        "mcp_server_ids": [*current, *missing],
    })
    print(f"    ✓ {agent} 绑定已补 servers={[*current, *missing]}")
    return [*current, *missing], True


def sync_custom_agent(api: Api, spec: dict, server_ids: dict[str, str]) -> tuple[set[str], list[str]]:
    """自定义 agent：**缺行就建**，已有行不碰 → ``(期望的 id 集合, 已存在但缺的绑定)``。

    与内置 agent 的不对称是刻意的：内置的行**整个意义就是携带绑定**（role/stage/prompt
    全在代码里），所以并集补绑定是在做这件事本身；而自定义的行**带着可本地编辑的
    prompt**，覆盖它会把别人在页面上改过的东西抹掉。所以这里只读不写。
    """
    name = spec["name"]
    row = api("GET", f"/agent-configs/{name}", allow_404=True)
    ids = [server_ids[n] for n in spec["server_names"] if n in server_ids]

    if row is not None:
        current = list((row or {}).get("mcp_server_ids") or [])
        lack = [i for i in ids if i not in current]
        print(f"  = 自定义 agent {name} 已存在（不动）servers={current}")
        if lack:
            print(
                f"      ⚠️ 它缺了种子声明的绑定 {lack} —— 本脚本**不会**替你改已有行\n"
                "         （prompt 是可本地改的）。但这**不是无害的**：该 agent 会**零工具**，\n"
                "         若它是 `ticket-done` 这类副作用节点，表现是**投递不出去、工单没有回音**。\n"
                "         要补请去 agent 页手工加，或删掉该行后重跑本脚本。"
            )
        return set(current), lack

    api("POST", "/agent-configs", {
        "name": name,
        "role": spec["role"],
        "stage": spec["stage"],
        "description": spec.get("description"),
        "system_prompt": spec["system_prompt"],
        "output_schema": spec.get("schema"),
        "mcp_server_ids": ids or None,
        "enabled": True,
    })
    print(f"  ✓ 自定义 agent {name} 已建（origin=custom）servers={ids}")
    return set(ids), []


def main() -> int:
    ap = argparse.ArgumentParser(description="把 seed 的数据面推到已开通租户")
    ap.add_argument("--tenant", required=True, help="目标租户 id（如 otr）")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="agentflow 控制面地址")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不发写请求")
    args = ap.parse_args()

    api = Api(args.base, args.tenant, args.dry_run)
    plane = load_dataplane_seed()
    customs = load_custom_agent_seeds()
    if not plane["servers"] and not customs:
        raise SystemExit("✗ 读不到任何 seed 数据面（agentflow/seed/dataplane.yaml）")

    print(f"== 把 seed 的数据面推到租户 {args.tenant} ==")
    print("\n[1/3] MCP server")
    server_ids = sync_servers(api, plane["servers"])
    # 种子声明了、但推完仍不在租户里的 server。**必须让它影响退出码**：那种情况下
    # 后面每个"绑不上"的 agent 都只是 warn + 跳过，最后会打出一个漂亮的
    # 「✓ 数据面就位：0 个内置 agent 绑定」并以 0 退出 —— 全盘失败伪装成成功。
    missing_servers = [s["name"] for s in plane["servers"] if s["name"] not in server_ids]

    # 期望集：agent → 推完之后它应该有的 server id 集合（并集语义下判"推成功"用子集）
    expected: dict[str, set[str]] = {}

    # 推前快照（自定义文本三件套）。**这是唯一能挡住"顺手把别人的提示词抹了"的东西**：
    # `PUT` 对 description / system_prompt / output_schema 是**无条件覆盖**，回传时漏一个键
    # （比如把 `output_schema` 写成 `schema`）就把它抹成 NULL —— 而绑定侧的回读
    # **完全看不见**这件事，一样会打 ✓。症状要等某条 run 的行为变了才出现。
    before_text = {
        r["name"]: (r.get("description"), r.get("system_prompt"), r.get("schema"))
        for r in api("GET", "/agent-configs")
    }

    print("\n[2/3] 内置 agent 绑定（并集：只加不删）")
    for agent, server_names in plane["bindings"].items():
        if agent not in _BUILTIN:
            # 与播种同一条判据（`seed/__init__.py` 的 `_agent_row`）：非内置名写进 bindings
            # 是**放错了地方**，该去 `seed/agents/*.yaml`。播种时只 warn 跳过，
            # 这里也一样 —— 但要说出来，否则表现为"少推了一个且不知道为什么"。
            print(f"  ⚠️ {agent} 不是内置 agent（应写进 seed/agents/*.yaml），跳过")
            continue
        want = [server_ids[n] for n in server_names if n in server_ids]
        unknown = [n for n in server_names if n not in server_ids]
        if unknown:
            print(f"  ⚠️ {agent} 声明的 server {unknown} 在租户里不存在，按未绑定处理")
        if not want:
            print(f"  ⚠️ {agent} 一个可绑的 server 都没有，跳过")
            continue
        got, wrote = sync_binding(api, agent, want)
        expected[agent] = set(got)
        if not wrote:
            print(f"    = {agent} 已绑定（跳过）servers={got}")

    print("\n[3/3] 自定义 agent")
    unbound: list[str] = []
    problems: list[str] = []
    for spec in customs:
        if spec["name"] in _BUILTIN:
            # 写错了地方（README 里那条"选择哪条"的约定）。**必须硬失败**：内置名逐名 GET
            # 返回的是**合成视图**而不是 404，不显式判就会被下一行读成"已存在（不动）"
            # —— 种子的意图被静默吞掉，与播种时"只有一条 warning 日志"是同一个坑。
            print(f"  ✗ {spec['name']} 是**内置** agent，不该写在 seed/agents/*.yaml 里")
            print("     它的 role/prompt/schema 都在代码里；绑定请写进 seed/dataplane.yaml 的 bindings。")
            problems.append(f"{spec['name']}: 内置 agent 写进了 seed/agents/*.yaml（写错地方）")
            continue
        ids, lack = sync_custom_agent(api, spec, server_ids)
        expected[spec["name"]] = ids
        if lack:
            unbound.append(f"{spec['name']}（缺 {lack}）")

    if args.dry_run:
        print("\n（dry-run：没有发任何写请求）")
        return 0

    # ── 推完回读校验 ──────────────────────────────────────────────
    #
    # 判据是**子集**而不是相等：并集语义下租户多出来的绑定（`otr` 的 `git-server`）
    # 是合法的，判相等会把它们误报成失败。
    print("\n== 回读校验（GET /agents，期望集 ⊆ 读回集） ==")
    actual = {a["name"]: set(a.get("mcp_server_ids") or []) for a in api("GET", "/agents")}
    bad: list[str] = [
        f"MCP server {n}: 种子里声明了但租户里没有（注册也没成功）" for n in missing_servers
    ]
    if missing_servers:
        print(f"  ✗ 缺 MCP server：{missing_servers}")
    for agent, want in expected.items():
        got = actual.get(agent)
        if got is None:
            bad.append(f"{agent}: /agents 里查不到这个 agent")
            print(f"  ✗ {agent}")
        elif not want <= got:
            bad.append(f"{agent}: 期望 {sorted(want)}，读回 {sorted(got)}（缺 {sorted(want - got)}）")
            print(f"  ✗ {agent}")
        else:
            print(f"  ✓ {agent}  servers={sorted(got)}")

    # 判据③：**碰过的行，它的自定义文本必须一字未动**。
    # `_acfg_str` 会 strip 后入库，所以按 strip 归一后再比 —— 不归一的话，一次
    # "写了同样的值"会被误报成改动，而**误报和漏报一样毁掉校验的可信度**。
    after_text = {
        r["name"]: (r.get("description"), r.get("system_prompt"), r.get("schema"))
        for r in api("GET", "/agent-configs")
    }
    for agent in expected:
        if agent not in before_text:
            continue  # 推前就没有行（本次新建）→ 没有"被抹掉"可言
        if _norm(before_text[agent]) != _norm(after_text.get(agent, _MISSING_TEXT)):
            bad.append(
                f"{agent}: 自定义文本被改动了（description/system_prompt/schema）"
                " —— PUT 漏回传某个键会把它抹成 NULL"
            )
            print(f"  ✗ {agent}  自定义文本被改动")

    bad.extend(problems)
    if bad:
        print("\n✗ 以下条目没推成功：")
        for b in bad:
            print(f"    {b}")
        return 1

    print(
        f"\n✓ 数据面就位：{len(expected)} 个内置 agent 绑定 + {len(customs)} 个自定义 agent"
    )
    if unbound:
        # 刻意**不改退出码**：这些行本脚本按设计不动，判失败会让它在这个租户上永远红着、
        # 而脚本自己又没有修它的手段。但也不能默认它没事 —— 零工具的自定义 agent
        # 正是"看着跑完了、其实没交付"那一类，所以说出口。
        print("\n⚠️ 以下自定义 agent **已存在但缺种子声明的绑定**（本脚本不碰已有行）：")
        for u in unbound:
            print(f"     · {u}")
        print("   它们会**零工具**——若涉及 ticket-done，工单不会有回音。请手工补。")
    print(
        "\n⚠️ 本脚本**只同步数据面**。workflow（`seed/workflows/*.yaml`）走："
        "\n     make sync-workflows TENANT=" + args.tenant + "\n"
        "   两条合起来才是完整的「把 seed 推到已开通租户」。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
