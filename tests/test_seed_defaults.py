"""新租户默认数据播种（`agentflow/seed/`）—— 幂等、不覆盖、绑定指向真实 id。

**背景**：`tenantctl provision` 原先只建库建表、不播种任何数据，新租户
`POST /tickets/{tid}/run` 直接 400。更隐蔽的是中间态：只播了 workflow 而不播
agent↔server 绑定的话，run 能跑完但**每个 agent 零工具**（"看着成功，实则空转"）。
所以三张表要一起验。
"""
from __future__ import annotations

import re
from functools import lru_cache

import pytest

from agentflow.agents.agent_config import AgentConfigResolver
from agentflow.agents.prompts import AGENT_SCHEMAS
from agentflow.agents.registry import DIAGNOSE_AGENTS, FIX_AGENTS
from agentflow.api.agent_store import AgentConfigStore
from agentflow.api.mcp_store import MCPStore
from agentflow.api.workflow_store import WorkflowStore
from agentflow.core.workflow import Workflow
from agentflow.seed import (
    load_custom_agent_seeds,
    load_dataplane_seed,
    load_workflow_seeds,
    seed_defaults,
)


@lru_cache(maxsize=1)
def _seed_agent_schemas() -> dict[str, dict]:
    """自定义 agent 的 name → 输出 schema（从种子里读，**不是**代码里的静态表）。

    `AGENT_SCHEMAS` 只覆盖内置 agent；自定义 agent（如 ticket-done）的 schema
    只存在于 seed/agents/*.yaml —— 而 `AgentConfigResolver` 会把它带进运行时，
    所以「引用它的输出字段是否真实存在」这条校验对它同样适用。
    """
    return {c["name"]: (c["schema"] or {}) for c in load_custom_agent_seeds()}


class _Settings:
    """播种器只用到这一个设置项（URL 由配置注入，不写死在种子里）。"""

    mcp_datasource_url = "http://127.0.0.1:8300/mcp"


async def _stores():
    w, m, a = WorkflowStore(":memory:"), MCPStore(":memory:"), AgentConfigStore(":memory:")
    for s in (w, m, a):
        await s.connect()
    return w, m, a


async def _seed():
    """三张空表播一遍 → ``(workflow_store, mcp_store, agent_store, counts)``。"""
    w, m, a = await _stores()
    counts = await seed_defaults(w, m, a, settings=_Settings())
    return w, m, a, counts


# ======================================================================
# 语料本身（CI 硬闸门）——种子坏了要在这里拦住，而不是等某个租户开出来才发现
# ======================================================================
def test_seed_corpus_is_valid_and_nonempty() -> None:
    """种子非空、id 唯一、每条 workflow 都能加载、每个绑定都指向内置 agent。

    这条同时兜住"package-data 配错导致 wheel 里没有 YAML"——那种情况下
    `load_*` 会返回空结构，**功能静默失效**，没有任何报错。
    """
    ws = load_workflow_seeds()
    assert ws, "workflow 种子为空——检查 agentflow/seed/workflows/ 是否被打进包里"
    ids = [w["id"] for w in ws]
    assert len(ids) == len(set(ids)), f"种子 id 重复: {ids}"
    assert all(i.startswith("seed-") for i in ids), "种子 id 必须以 seed- 开头（一眼可辨来源）"
    for w in ws:
        assert Workflow.load_yaml(w["yaml"]).dag.nodes, f"{w['id']} 加载后没有节点"

    plane = load_dataplane_seed()
    assert plane["servers"], "dataplane 种子里没有 server"
    assert plane["bindings"], "dataplane 种子里没有 agent 绑定"
    builtin = set(DIAGNOSE_AGENTS) | set(FIX_AGENTS)
    unknown = set(plane["bindings"]) - builtin
    assert not unknown, f"绑定了非内置 agent：{sorted(unknown)}（新租户跑到那个节点会 KeyError）"
    server_names = {s["name"] for s in plane["servers"]}
    for agent, names in plane["bindings"].items():
        assert set(names) <= server_names, f"{agent} 绑了未声明的 server: {names}"

    # 自定义 agent 种子（seed/agents/*.yaml）：代码里没有副本，这里就是它的真源
    customs = load_custom_agent_seeds()
    assert customs, "自定义 agent 种子为空——检查 agentflow/seed/agents/ 是否被打进包里"
    names = [c["name"] for c in customs]
    assert len(names) == len(set(names)), f"自定义 agent 重名: {names}"
    for c in customs:
        assert c["role"] in ("diagnose", "fix"), f"{c['name']} 的 role 不在白名单"
        assert c["system_prompt"].strip(), f"{c['name']} 没有 system_prompt"
        assert c["schema"], f"{c['name']} 没有 output_schema"
        assert set(c["server_names"]) <= server_names, (
            f"{c['name']} 绑了未声明的 server: {c['server_names']}"
        )
        # 与内置 agent 撞名会让 POST /agent-configs 400（"是内置 agent"），
        # 而种子走的是 insert_if_absent、不校验——只能在这里拦
        assert c["name"] not in builtin, f"自定义 agent {c['name']} 与内置重名"

    # workflow 引用的 agent 必须在内置 ∪ 自定义里：`Workflow.load_yaml` **不查**
    # agent 是否存在，引到不存在的名字要到第一次 run 才炸
    known = builtin | set(names)
    for w in ws:
        used = {n.agent for n in Workflow.load_yaml(w["yaml"]).nodes.values() if n.agent}
        unknown = sorted(used - known)
        assert not unknown, f"{w['id']} 引用了未知 agent: {unknown}"


def test_seed_params_reference_real_schema_fields() -> None:
    """种子里每个 ``$.nodes.<id>.output.<field>`` 都必须指向**真实存在**的字段。

    为什么值一条测试：``params`` 引用不存在的字段**不报错、不加载失败**，只是**恒解析为
    None**——节点照跑、run 照报 done，只有去核数据形状才发现入参是空的。

    实际踩到过：两个 workflow 的 ``recap`` 都写 ``status: "$.nodes.commit.status"``
    （注意**连 ``.output`` 访问器都没有**），而 ``CommitSchema`` 里没有任何叫 ``status``
    的字段（只有 pr_url / pr_number / base_sha）——``_walk`` 遇失配键返回 None，
    实测 20+ 条 run 该入参**全是 null**，"这次提交到底成没成"从来没到达复盘 agent。
    这条测试就是那次漏网的补网。

    两种写法都要查：``$.nodes.X.output.<field>`` 与漏了访问器的 ``$.nodes.X.<field>``
    ——后者在前缀剥离后**同样**是在节点输出字典里查键（`_resolve_param` 只在
    ``field.startswith("output")`` 时才剥前缀），所以用同一套 schema 判据即可。
    """
    pat = re.compile(r"^\$\.nodes\.([\w-]+)(?:\.(.+))?$")

    checked = 0
    for w in load_workflow_seeds():
        dag = Workflow.load_yaml(w["yaml"]).dag
        for nid, node in dag.nodes.items():
            for pname, path in (node.params or {}).items():
                if not isinstance(path, str):
                    continue
                m = pat.match(path.strip())
                if m is None:
                    continue
                src, rest = m.group(1), m.group(2)
                assert src in dag.nodes, f"{w['id']} {nid}.{pname} 引用了不存在的节点 {src}"
                if not rest:
                    continue  # `$.nodes.X` → 整个输出，无字段可查
                field = rest
                if field.startswith("output"):
                    field = field[len("output"):].lstrip(".")
                if not field or field.startswith("["):
                    continue  # 整个 output / 纯下标：取的是值本身，非字段
                head = field.split(".")[0].split("[")[0]
                if not head:
                    continue
                agent = dag.nodes[src].agent
                # **两种来源**：内置 agent 的 schema 在代码里（AGENT_SCHEMAS），
                # 自定义 agent（如 ticket-done）的只在种子里 —— 不纳入的话，
                # 任何引用自定义 agent 输出的 params 都会在这里误报"没有输出 schema"。
                schema = AGENT_SCHEMAS.get(agent) or _seed_agent_schemas().get(agent)
                assert schema is not None, (
                    f"{w['id']} {nid}.{pname}：{src} 的 agent={agent!r} 没有输出 schema，"
                    "无法核对字段是否存在"
                )
                assert head in schema.get("properties", {}), (
                    f"{w['id']} {nid}.{pname} -> {path}：字段 {head!r} 不在 {agent} 的"
                    f"输出 schema 里（可用：{sorted(schema.get('properties', {}))}）"
                    "——引用不存在的字段不会报错，只会**恒为 null**"
                )
                checked += 1
    assert checked > 0, "一条 params 引用都没查到——正则或种子结构变了，本测试已失效"


def test_seed_missing_source_is_noop(monkeypatch) -> None:
    """种子文件读不到 → 空结构，**不抛**（fail-soft：本模块在请求路径上）。"""
    import agentflow.seed as seed_mod

    monkeypatch.setattr(seed_mod, "_read", lambda _rel: None)
    assert seed_mod.load_workflow_seeds() == []
    assert seed_mod.load_dataplane_seed() == {"servers": [], "bindings": {}}


# ======================================================================
# 播种语义
# ======================================================================
async def test_seed_empty_tables_write_all() -> None:
    """三张空表 → 各写入种子声明的条数（**从 loader 派生，不硬编码**）。"""
    w, m, a, counts = await _seed()
    assert counts == {
        "workflows": len(load_workflow_seeds()),
        "servers": len(load_dataplane_seed()["servers"]),
        # agents 表现在有**两个来源**：内置 agent 的绑定 + 自定义 agent 的完整定义
        "agents": len(load_dataplane_seed()["bindings"]) + len(load_custom_agent_seeds()),
    }
    assert {r["id"] for r in await w.list()} == {x["id"] for x in load_workflow_seeds()}
    assert {s["name"] for s in await m.list()} == {
        s["name"] for s in load_dataplane_seed()["servers"]
    }
    assert {r["name"] for r in await a.list()} == (
        set(load_dataplane_seed()["bindings"]) | {c["name"] for c in load_custom_agent_seeds()}
    )


async def test_custom_agent_is_seeded_with_its_own_prompt_and_schema() -> None:
    """自定义 agent（seed/agents/*.yaml）要**连 prompt 与 schema 一起**播进库。

    与内置 agent 的绑定行不同：内置行的 prompt/schema 留空（走代码里的静态回退，
    避免双真源），而自定义 agent 代码里没有副本 —— 留空 = 真·没有提示词，
    节点会退化成 `scopes.build_agent` 里那句兜底「你是 AI 运维平台智能体。」
    然后产出不可解析的输出。
    """
    _w, _m, a, _ = await _seed()
    rows = {r["name"]: r for r in await a.list()}
    for spec in load_custom_agent_seeds():
        row = rows[spec["name"]]
        assert row["origin"] == "custom", f"{spec['name']} 的 origin 应为 custom"
        assert row["role"] == spec["role"] and row["stage"] == spec["stage"]
        # 比 **strip 过**的值：store 写入时 `_opt_str` 会去首尾空白（YAML 的 `|`
        # 块标量自带尾换行，故意保留它没有意义），差异仅此而已
        assert row["system_prompt"] == spec["system_prompt"].strip(), "prompt 没播进去"
        assert row["schema"] == spec["schema"], "schema 没原样播进去"
    # 而内置绑定的行仍**不带** prompt（走静态回退）——这条是反向对照
    assert rows["log-analyst"]["system_prompt"] is None


async def test_seed_is_idempotent() -> None:
    """连播两次：第二次全 0，且三表内容逐字节不变。"""
    w, m, a, _ = await _seed()
    before = ((await w.list()), (await m.list()), (await a.list()))

    again = await seed_defaults(w, m, a, settings=_Settings())
    assert again == {"workflows": 0, "servers": 0, "agents": 0}
    assert ((await w.list()), (await m.list()), (await a.list())) == before


async def test_seed_leaves_non_empty_tables_untouched() -> None:
    """每张表**各自**判断"空不空"——已有数据的那张不动，空的照播。"""
    w, m, a = await _stores()
    mine = await w.save("我自己的流程", "name: mine\nnodes: {}\nedges: []\n")

    await seed_defaults(w, m, a, settings=_Settings())

    rows = await w.list()
    assert [r["id"] for r in rows] == [mine]  # workflow 表没被碰
    assert await m.list() and await a.list()  # 另外两张照播


async def test_seed_does_not_overwrite_existing_rows() -> None:
    """播种**绝不覆盖**已有行：预置的三条（各表一条）内容一字不变。

    走的是"空表才播"这道守卫——表非空 → 整表跳过 → 连 insert 都不会调。
    注意它**不覆盖** `insert_if_absent` 的冲突分支（那条走不到），后者的行为由
    下面 `test_insert_if_absent_never_overwrites` 直接钉。

    三张表各测一遍：它们的唯一约束不同（workflows 是 id，另两张是 name），
    只测一张会漏掉另外两张。
    """
    w, m, a = await _stores()
    seed_w = load_workflow_seeds()[0]
    # 直接按种子 id 写一条"租户改过的"，模拟"播种前就存在同 id 行"
    await w.insert_if_absent(seed_w["id"], "租户改过的名字", seed_w["yaml"], "2020-01-01T00:00:00+00:00")
    await m.insert_if_absent({
        "id": "someone-elses-id", "name": "aiops-datasource", "transport": "http",
        "config": {"url": "http://elsewhere:9999/mcp"}, "is_stateful": 0, "enabled": True,
    })
    await a.insert_if_absent({"name": "log-analyst", "origin": "builtin", "role": "diagnose",
                        "stage": "detect", "mcp_server_ids": ["someone-elses-id"]})

    await seed_defaults(w, m, a, settings=_Settings())

    by_id = {r["id"]: r for r in await w.list()}
    assert by_id[seed_w["id"]]["name"] == "租户改过的名字"  # 没被种子的 name 覆盖
    servers = await m.list()
    assert [s["id"] for s in servers] == ["someone-elses-id"]  # name 撞车 → 种子那条没插进去
    agents = {r["name"]: r for r in await a.list()}
    assert agents["log-analyst"]["mcp_server_ids"] == ["someone-elses-id"]


async def test_binding_points_at_the_real_server_id() -> None:
    """**绑定必须指向库里真实存在的 server id**，而不是种子里那个假想 id。

    `mcp_servers` 的唯一约束在 `name`：租户若已有同名 server，种子的插入会被
    `ON CONFLICT` 吞掉。此时若还按 `seed-<name>` 写绑定，就会绑到一个**不存在的
    server**——症状是静默零工具，没有任何报错。所以必须按名读回真实 id。
    """
    w, m, a = await _stores()
    existing = await m.save({"name": "aiops-datasource", "transport": "http",
                             "config": {"url": "http://tenant-own:8300/mcp"}})

    await seed_defaults(w, m, a, settings=_Settings())

    rows = {r["name"]: r for r in await a.list()}
    bound = load_dataplane_seed()["bindings"]
    assert bound, "dataplane 种子里没有绑定"
    for agent in bound:
        assert agent in rows, f"{agent} 的绑定行没播上"
        assert rows[agent]["mcp_server_ids"] == [existing], (
            f"{agent} 绑到了 {rows[agent]['mcp_server_ids']}，应为库里真实的 {existing!r}"
        )
    # 自定义 agent **故意不在**这条断言里：它们目前不绑任何 server（没有出站工具可绑），
    # 播出来的 `mcp_server_ids` 是 None —— 那不等于"播漏了"，见 seed/agents/*.yaml


async def test_binding_makes_agents_usable_and_triage_stays_toolless() -> None:
    """端到端语义：播完之后 resolver 真的解析得出工具；`triage` 仍为空集。

    这条是整件事的目的——**"配置就位"要能兑现成"agent 拿得到工具"**，
    而不只是表里有行。
    """
    _w, _m, a, _ = await _seed()
    resolver = AgentConfigResolver(await a.list())

    bound = load_dataplane_seed()["bindings"]
    for agent in bound:
        assert resolver.server_ids_for(agent), f"{agent} 播完仍无工具"
    # triage 有意不绑（"triage 交出数据工具"）。没有 DB 行 = 静态回退 = 空集。
    assert resolver.server_ids_for("triage") == set()
    # 提示词走静态回退（种子里不塞 prompt，避免与代码构成双真源）
    assert resolver.resolve("log-analyst").system_prompt


async def test_workflow_default_is_the_last_manifest_entry() -> None:
    """`list()` 顺序 == manifest 顺序 ⇒ **末条 = 默认流程**（`saved[0]`）。

    顺序靠 `created_at` 递增 1ms 保证；没有它，同批写入的 created_at 可能撞在同一
    微秒，`ORDER BY created_at DESC` 无二级排序 → "默认是哪条"就是掷骰子。
    """
    w, _m, _a, _ = await _seed()
    entries = load_workflow_seeds()
    assert (await w.list())[0]["id"] == entries[-1]["id"]


async def test_insert_if_absent_never_overwrites() -> None:
    """直接钉 store 层的冲突分支：同 id / 同 name 再插一次 → False 且内容不变。

    播种器那条测试**走不到这里**（"空表才播"让它连 insert 都不会调），所以必须单独测——
    否则把 `ON CONFLICT DO NOTHING` 删掉都不会有任何测试失败，而它正是并发
    `TenantStoresRouter._build()`（缓存未命中时两个请求各建一遍）撞车时的唯一兜底。
    """
    w, m, a = await _stores()
    # workflows：唯一约束在 id
    assert await w.insert_if_absent("seed-x", "原名", "name: x\n", "2020-01-01T00:00:00+00:00")
    assert await w.insert_if_absent("seed-x", "改过的", "name: y\n", "2021-01-01T00:00:00+00:00") is False
    row = next(r for r in await w.list() if r["id"] == "seed-x")
    assert row["name"] == "原名" and (await w.get("seed-x"))["yaml"] == "name: x\n"

    # mcp_servers / agent_configs：唯一约束在 name
    srv = {"id": "id-1", "name": "s1", "transport": "http", "config": {"url": "u"}}
    assert await m.insert_if_absent(srv) == "id-1"
    assert await m.insert_if_absent({**srv, "id": "id-2"}) is None  # name 撞 → 不插
    assert [s["id"] for s in await m.list()] == ["id-1"]

    row_a = {"name": "a1", "origin": "builtin", "role": "diagnose", "stage": "detect"}
    assert await a.insert_if_absent({**row_a, "mcp_server_ids": ["id-1"]}) is True
    assert await a.insert_if_absent({**row_a, "mcp_server_ids": []}) is False
    assert (await a.list())[0]["mcp_server_ids"] == ["id-1"]


# ======================================================================
# 两端方法面 parity（PG 零覆盖下唯一能防"只加了一半"的测试）
# ======================================================================
@pytest.mark.parametrize("method", ["insert_if_absent"])
def test_insert_if_absent_exists_on_both_backends(method: str) -> None:
    """sqlite 与 PG 两个实现都必须有 `insert_if_absent`。

    本仓的 PG 分支**零测试覆盖**（没有 PG 容器、没有 skip 标记），所以"只给 sqlite
    加了方法、PG 忘了"这种错在测试里完全看不出来——只能靠内省把两端钉在一起。
    """
    from agentflow.api.agent_store import PgAgentConfigStore
    from agentflow.api.mcp_store import PgMCPStore
    from agentflow.api.workflow_store import PgWorkflowStore

    for sqlite_cls, pg_cls, label in (
        (WorkflowStore, PgWorkflowStore, "workflow"),
        (MCPStore, PgMCPStore, "mcp"),
        (AgentConfigStore, PgAgentConfigStore, "agent"),
    ):
        assert hasattr(sqlite_cls, method), f"{label} 的 sqlite 实现缺 {method}"
        assert hasattr(pg_cls, method), f"{label} 的 PG 实现缺 {method}"
