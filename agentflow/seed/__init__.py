"""租户默认数据播种——**种子 ≠ 真源**（CLAUDE.md §6.0）。

新租户的库建好之后是**空的**，后果分三层，一层比一层隐蔽：

1. `workflows` 空 → `POST /tickets/{tid}/run` 直接 400（"库里没有已保存的 workflow"）
2. 即便播了 workflow，`mcp_servers` 空 → agent 绑定不到任何 server
3. `agent_configs.mcp_server_ids` 为 NULL → **每个 agent 零工具**，run 会跑完，
   但每个节点都在"无证据推理"——**看着成功，实则空转**

所以三张表要一起播，才叫"开箱可用"。

## 种子在哪、为什么在仓库里

```
agentflow/seed/workflows/_manifest.yaml + *.yaml    # workflow 种子
agentflow/seed/dataplane.yaml                       # MCP server + agent 绑定
```

**它们是种子，不是真源。真源是数据库。** 只在目标表**为空**时写入一次；
改了这里**对已存在的租户没有任何效果**（要改已开通租户走 `PUT /workflows/{wid}`）。
id 一律 `seed-` 前缀（`save()` 产出的是 12 位 hex，永不可能撞上）——一眼可辨来源。

设计取舍见 `docs/TODO.md` §13：另一条路是"从参考租户复制"，被否掉的原因是
**参考租户是可变状态**——谁改了它、或 `deprovision` 删了库，所有未来新租户拿到的默认
就跟着变，且没有 diff 面、没有 review 入口、没有版本可追溯；种子文件能进 PR review、
能 `git log`、能离线校验。

## 几条硬约束

- **fail-soft**：本模块被 `TenantStoresRouter._build()` 调用，而 `_build()` 在任何请求
  路径上。**任何异常都不能往上冒**——目录缺失 / manifest 坏 / YAML 不合法一律只 warn +
  跳过，否则一个新租户的首个请求会把整个进程带崩。
- **不缓存**：加 `lru_cache` 会让"改了种子文件不重启不生效"，那正是 §6.0 花力气在防的事。
- **不 import `api/`**：三个 store 由调用方传入，本模块只按鸭子类型调
  `list()` / `insert_if_absent()`——`seed/` 因此不加重 `TODO.md` §14 的分层债。
- **"空表才播"是 per-table 的**：某张表已有数据就整表不动。推论是：**把某张表清空的租户，
  会在下次进程启动 / LRU 重建时重新拿到种子**（"空 = 出厂态"）。逃生阀：
  `settings.seed_defaults=False`（`AGENTFLOW_SEED_DEFAULTS=0`）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml

log = logging.getLogger("agentflow.seed")

_PKG = "agentflow.seed"
_WORKFLOW_DIR = "workflows"
_WORKFLOW_MANIFEST = "_manifest.yaml"
_DATAPLANE = "dataplane.yaml"


def _read(relative: str) -> str | None:
    """读包内文本；失败 → None（fail-soft，见模块 docstring）。

    用 ``importlib.resources`` 而不是 ``Path(__file__).parent``：后者在 zip/zipapp 安装下
    失效，且对"包有没有被正确打包"没有任何反馈。
    """
    try:
        return files(_PKG).joinpath(relative).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError, UnicodeDecodeError) as exc:
        log.warning("[seed] 读取种子文件 %s 失败（跳过）: %s", relative, exc)
        return None


def load_workflow_seeds() -> list[dict[str, str]]:
    """→ ``[{id, name, yaml}]``，**按 manifest 顺序**（最后一条 = 新租户的默认流程）。

    任何一条坏了就跳过它、不拖累其余；整份读不出来 → ``[]``。

    顺序为什么是语义：``POST /tickets/{tid}/run`` 未指定 `workflow_id` 时取 ``saved[0]``
    （`api/app.py`），而 ``list()`` 是 ``ORDER BY created_at DESC`` 无二级排序——
    播种时按序给 ``created_at`` 递增（见 :func:`seed_defaults`）才能让"谁当默认"是确定的。
    """
    raw = _read(f"{_WORKFLOW_DIR}/{_WORKFLOW_MANIFEST}")
    if raw is None:
        return []
    try:
        items = (yaml.safe_load(raw) or {}).get("items") or []
    except (yaml.YAMLError, AttributeError) as exc:
        log.warning("[seed] workflow manifest 解析失败（跳过）: %s", exc)
        return []

    out: list[dict[str, str]] = []
    for item in items:
        wid, fname = (item or {}).get("id"), (item or {}).get("file")
        if not wid or not fname:
            log.warning("[seed] manifest 条目缺 id/file（跳过）: %r", item)
            continue
        text = _read(f"{_WORKFLOW_DIR}/{fname}")
        if text is None:
            continue
        # 在这里就验证能不能加载：宁可少播一条，也不要把坏流程写进新租户的库
        # （写进去了要到第一次 run 才炸，且那时已经看不出是种子的问题）。
        try:
            from ..core.workflow import Workflow

            Workflow.load_yaml(text)
            name = (yaml.safe_load(text) or {}).get("name")
        except Exception as exc:  # noqa: BLE001 - 坏种子只跳过，绝不外抛
            log.warning("[seed] %s 不是合法 workflow（跳过）: %s", fname, exc)
            continue
        if not name:
            log.warning("[seed] %s 缺 name 字段（跳过）", fname)
            continue
        out.append({"id": wid, "name": str(name), "yaml": text})
    return out


def load_dataplane_seed() -> dict[str, Any]:
    """→ ``{"servers": [...], "bindings": {agent: [server_name, ...]}}``；失败 → 空结构。"""
    empty: dict[str, Any] = {"servers": [], "bindings": {}}
    raw = _read(_DATAPLANE)
    if raw is None:
        return empty
    try:
        doc = yaml.safe_load(raw) or {}
        return {
            "servers": list(doc.get("servers") or []),
            "bindings": dict(doc.get("bindings") or {}),
        }
    except (yaml.YAMLError, AttributeError, TypeError) as exc:
        log.warning("[seed] dataplane.yaml 解析失败（跳过）: %s", exc)
        return empty


def _server_row(spec: dict, url: str) -> dict:
    """dataplane 的 server 声明 → ``mcp_store.insert_if_absent`` 的入参。

    **不写 `tools` / `enable_tools` / `disable_tools`**：那几列是 MCP server 被 load 时
    **运行时发现**的结果，写进种子等于把一次性的发现冻成声明，服务端工具一改就与事实不符。
    """
    return {
        "id": f"seed-{spec['name']}",
        "name": spec["name"],
        "transport": spec.get("transport", "http"),
        "config": {"url": url},
        "is_stateful": spec.get("is_stateful", 0),
        "enabled": True,
    }


def _agent_row(name: str, server_ids: list[str]) -> dict | None:
    """agent 名 + 绑定的 server id → ``agent_store.insert_if_absent`` 的入参。

    role/stage **从静态注册表取**（`AgentConfigResolver` 空行集 → 全部走静态回退），
    不在种子里重复一份——重复就会漂移。顺带这也校验了"该 agent 是不是内置的"：
    非内置返回 None。

    `system_prompt` / `schema` 留空走静态回退：把提示词也塞进种子等于与代码里那份
    构成双真源。
    """
    from ..agents.agent_config import AgentConfigResolver

    if not server_ids:
        return None
    try:
        resolved = AgentConfigResolver([]).resolve(name)
    except Exception as exc:  # noqa: BLE001 - 静态表读不到只跳过这一条
        log.warning("[seed] 解析 agent %s 的静态默认失败（跳过）: %s", name, exc)
        return None
    if resolved is None:
        return None
    return {
        "name": name,
        "origin": "builtin",
        "role": resolved.role,
        "stage": resolved.stage,
        "mcp_server_ids": server_ids,
    }


async def seed_defaults(
    workflow_store: Any, mcp_store: Any, agent_store: Any, *, settings: Any
) -> dict[str, int]:
    """把种子写进一个**新租户**的库；返回各表本次写入条数。

    ``{workflow_store, mcp_store, agent_store}`` 由调用方传入（**不要在这里 import
    `api/`，也不要自己构造 store**——构造必须经 `TenantStoresRouter`，否则会落到共享
    基础库、跨租户可见）。

    三张表**各自独立**判断"是不是空的"：新租户三张都空，结果一致；已有数据的租户不被碰。
    """
    counts = {"workflows": 0, "servers": 0, "agents": 0}

    # ── ① workflows ──────────────────────────────────────────────
    try:
        if not await workflow_store.list():
            base = datetime.now(UTC)
            for i, item in enumerate(load_workflow_seeds()):
                # created_at 递增 1ms：保证 list() 的顺序 == manifest 顺序，
                # 于是 manifest 最后一条 = saved[0] = 新租户的默认流程（确定，不是掷骰子）。
                ts = (base + timedelta(milliseconds=i)).isoformat()
                if await workflow_store.insert_if_absent(
                    item["id"], item["name"], item["yaml"], ts
                ):
                    counts["workflows"] += 1
    except Exception:
        log.exception("[seed] 播种 workflow 失败（跳过）")

    # ── ② mcp_servers ────────────────────────────────────────────
    plane = load_dataplane_seed()
    try:
        if not await mcp_store.list():
            for spec in plane["servers"]:
                setting_name = spec.get("url_setting")
                url = getattr(settings, setting_name, None) if setting_name else spec.get("url")
                if not url:
                    log.warning(
                        "[seed] server %s 的 url_setting=%r 在配置里取不到值（跳过）",
                        spec.get("name"), setting_name,
                    )
                    continue
                if await mcp_store.insert_if_absent(_server_row(spec, url)):
                    counts["servers"] += 1
    except Exception:
        log.exception("[seed] 播种 MCP server 失败（跳过）")

    # ── ③ agent 绑定 ─────────────────────────────────────────────
    try:
        # **按名读回真实 id**，不能假定 id 就是 `seed-<name>`：`mcp_servers` 的唯一约束在
        # `name`，租户若已有同名 server，②的插入会被 `ON CONFLICT` 吞掉，此时绑定必须
        # 指向那个**已存在**的 id——否则绑到一个不存在的 server，症状是静默零工具。
        server_ids = {s["name"]: s["id"] for s in await mcp_store.list()}
        if not await agent_store.list():
            for agent, names in plane["bindings"].items():
                ids = [server_ids[n] for n in names if n in server_ids]
                if not ids:
                    log.warning("[seed] agent %s 绑定的 server 一个都不存在（跳过）", agent)
                    continue
                row = _agent_row(agent, ids)
                if row is None:
                    log.warning(
                        "[seed] agent %r 不是内置 agent（静态注册表查不到），跳过绑定", agent
                    )
                    continue
                if await agent_store.insert_if_absent(row):
                    counts["agents"] += 1
    except Exception:
        log.exception("[seed] 播种 agent 绑定失败（跳过）")

    return counts
