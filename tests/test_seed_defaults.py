"""新租户默认数据播种（`agentflow/seed/`）—— 幂等、不覆盖、绑定指向真实 id。

**背景**：`tenantctl provision` 原先只建库建表、不播种任何数据，新租户
`POST /tickets/{tid}/run` 直接 400。更隐蔽的是中间态：只播了 workflow 而不播
agent↔server 绑定的话，run 能跑完但**每个 agent 零工具**（"看着成功，实则空转"）。
所以三张表要一起验。
"""
from __future__ import annotations

import pytest

from agentflow.agents.agent_config import AgentConfigResolver
from agentflow.agents.registry import DIAGNOSE_AGENTS, FIX_AGENTS
from agentflow.api.agent_store import AgentConfigStore
from agentflow.api.mcp_store import MCPStore
from agentflow.api.workflow_store import WorkflowStore
from agentflow.core.workflow import Workflow
from agentflow.seed import load_dataplane_seed, load_workflow_seeds, seed_defaults


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
        "agents": len(load_dataplane_seed()["bindings"]),
    }
    assert {r["id"] for r in await w.list()} == {x["id"] for x in load_workflow_seeds()}
    assert {s["name"] for s in await m.list()} == {
        s["name"] for s in load_dataplane_seed()["servers"]
    }
    assert {r["name"] for r in await a.list()} == set(load_dataplane_seed()["bindings"])


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

    rows = await a.list()
    assert rows, "agent 绑定没播上"
    for r in rows:
        assert r["mcp_server_ids"] == [existing], (
            f"{r['name']} 绑到了 {r['mcp_server_ids']}，应为库里真实的 {existing!r}"
        )


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
