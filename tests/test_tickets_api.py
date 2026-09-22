"""B2：Ticket Inbox 端点（建/列/查/发起 run）。

夹具同 test_runs_list_api.py，另需 monkeypatch app.ticket_store。
"""
import asyncio

import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app
from agentflow.api.ticket_store import TicketStore
from agentflow.api.workflow_store import WorkflowStore
from agentflow.service import RunService
from agentflow.statestore.sqlite import SqliteStateStore

# 用测试数据里的真实形状（DIAGNOSE_TEST_GUIDE §BUG）
BUG_REPORT = {
    "number": "INC0012345",
    "short_description": "订单服务结账无响应",
    "cmdb_ci": {"name": "order-service", "namespace": "order"},
    "correlation_hint": {"trace_id": "trace-20260819-143000-abc123"},
}

VALID_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug_report" } }
edges: []
"""

#: 一个**读工单里那份诊断**的图 —— 修复流程的形态（plan 靠它拿根因与方案）。
READS_DIAGNOSIS_YAML = """
name: fix-flow
nodes:
  plan: { agent: fix-planner, params: { rca: "$.inputs.bug_report.diagnosis.rca" } }
edges: []
"""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture
async def stores(tmp_path, monkeypatch):
    ws = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", ws)
    ts = TicketStore(tmp_path / "tickets.db")
    await ts.connect()
    monkeypatch.setattr(app_mod, "ticket_store", ts)
    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    monkeypatch.setattr(app_mod, "service", RunService(store))
    return {"workflow": ws, "ticket": ts}


async def _create(client, **over) -> dict:
    body = {
        "title": "订单服务结账无响应",
        "bug_report": BUG_REPORT,
        "window_start": "2026-08-19T14:00:00+08:00",
        "window_end": "2026-08-19T15:00:00+08:00",
    }
    body.update(over)
    resp = await client.post("/tickets", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_ticket_derives_fields_from_bug_report(stores) -> None:
    async with _client() as client:
        t = await _create(client)

    assert t["id"] and len(t["id"]) == 12
    assert t["title"] == "订单服务结账无响应"
    # number / service / namespace 从 bug_report 里兜底取
    assert t["number"] == "INC0012345"
    assert t["service"] == "order-service"
    assert t["namespace"] == "order"
    assert t["status"] == "new"
    assert t["run_ids"] == []
    # inputs 就是可直接喂给 workflow 的形状
    assert t["inputs"]["bug_report"]["number"] == "INC0012345"
    assert t["inputs"]["window_start"] == "2026-08-19T14:00:00+08:00"
    assert t["created_at"] and t["updated_at"]


async def test_explicit_fields_override_bug_report(stores) -> None:
    async with _client() as client:
        t = await _create(client, number="MANUAL-1", service="other-svc")

    assert t["number"] == "MANUAL-1"
    assert t["service"] == "other-svc"


async def test_window_omitted_when_not_given(stores) -> None:
    """没给时间窗时不写 None —— workflow 的 window_start/end 是 required，缺键报错更直白。"""
    async with _client() as client:
        t = await _create(client, window_start=None, window_end=None)

    assert "window_start" not in t["inputs"]
    assert "window_end" not in t["inputs"]


async def test_list_tickets_and_status_filter(stores) -> None:
    async with _client() as client:
        t1 = await _create(client, title="A")
        await _create(client, title="B")

        rows = (await client.get("/tickets")).json()
        assert len(rows) == 2
        assert (await client.get("/tickets?status=new")).json().__len__() == 2
        assert (await client.get("/tickets?status=resolved")).json() == []

        one = (await client.get(f"/tickets/{t1['id']}")).json()
        assert one["id"] == t1["id"]


async def test_ticket_tenant_isolation(stores) -> None:
    async with _client() as client:
        t = await _create(client)

        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        async with other:
            assert (await other.get("/tickets", headers={"X-Tenant-ID": "t2"})).json() == []
            # 跨租户取单条 → 404（不泄漏存在性）
            assert (
                await other.get(f"/tickets/{t['id']}", headers={"X-Tenant-ID": "t2"})
            ).status_code == 404


async def test_run_ticket_creates_run_and_links_back(stores) -> None:
    async with _client() as client:
        wid = (await client.post(
            "/workflows", json={"name": "simple-flow", "yaml": VALID_YAML}
        )).json()["id"]
        t = await _create(client)

        resp = await client.post(f"/tickets/{t['id']}/run", json={})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["ticket_id"] == t["id"]
        assert out["run_id"].startswith("run_")
        assert out["workflow_id"] == wid  # 未指定 → 兜底用第一个已保存流程

        # run 挂回工单，状态转为 running
        after = (await client.get(f"/tickets/{t['id']}")).json()
        assert after["run_ids"] == [out["run_id"]]
        assert after["status"] == "running"

        # 该 run 出现在 B1 的列表里，且 inputs 与工单一致
        runs = (await client.get("/runs")).json()
        assert [r["run_id"] for r in runs] == [out["run_id"]]
        assert runs[0]["inputs"]["bug_report"]["number"] == "INC0012345"


async def test_run_ticket_strips_diagnosis_from_run_inputs(stores) -> None:
    """目标图**不读**诊断时，把它剥掉再下发（图读的情形见下一条）。

    工单详情页要显示 `bug_report.diagnosis`（建单节点写进去的），而工单的 inputs 又是下一次
    run 的 inputs —— 不剥的话，`triage` / `logs` 的 `bug: "$.inputs.bug_report"` 会**整个
    dict 交给模型**，于是重跑同一张单时模型先读到上一次的结论。
    """
    async with _client() as client:
        await client.post("/workflows", json={"name": "simple-flow", "yaml": VALID_YAML})
        t = await _create(
            client,
            bug_report={**BUG_REPORT, "diagnosis": {"rca": {"root_cause_type": "code_bug"}}},
        )
        # 工单自己留着诊断（详情页靠它显示）
        kept = (await client.get(f"/tickets/{t['id']}")).json()
        assert kept["inputs"]["bug_report"]["diagnosis"]

        out = (await client.post(f"/tickets/{t['id']}/run", json={})).json()
        run = next(r for r in (await client.get("/runs")).json() if r["run_id"] == out["run_id"])
        # 下发给 run 的那份剥掉了，其余字段原样
        assert "diagnosis" not in run["inputs"]["bug_report"]
        assert run["inputs"]["bug_report"]["number"] == "INC0012345"


def _wf_yaml(name: str) -> str:
    """同一张图，只换流程名（`insert_if_absent` 的 name 与 YAML 里的 name 都对上）。"""
    return VALID_YAML.replace("simple-flow", name)


# ======================================================================
# 建单时钉住 workflow（`POST /tickets` 的 workflow_id）
# ======================================================================
async def test_create_ticket_pins_the_workflow_by_name(stores) -> None:
    """建单传 `workflow_id` → 存进 `workflow_name` 列的是那条流程的**名字**，不是 id。

    「收 id、存名字」是刻意的（见 `create_ticket` 的 docstring）：`workflows.name` 没有唯一
    约束，收名字的话重名时指不准是哪一条；而列里**必须**存名字，因为运行期
    `_workflow_for_ticket` 是按名字查回 id 的，且名字才是跨租户可移植的键。
    """
    async with _client() as client:
        wid = (
            await client.post(
                "/workflows", json={"name": "pinned-flow", "yaml": _wf_yaml("pinned-flow")}
            )
        ).json()["id"]
        t = await _create(client, workflow_id=wid)
        assert t["workflow_name"] == "pinned-flow"
        # 回读一遍：不能只是 201 响应里对了
        assert (await client.get(f"/tickets/{t['id']}")).json()["workflow_name"] == "pinned-flow"


async def test_create_ticket_404_when_workflow_id_unknown(stores) -> None:
    """钉一个不存在的 workflow → 404，且**一张单都不建**。

    校验必须发生在 `tickets.create` **之前**。放到之后的话，会留下一张"钉了个不存在的
    流程"的废单，要等点「发起诊断」才 409 —— 那时单已经建出来了，而界面上**看不出**
    它是一张废单（钉住的列不显示在列表里）。
    """
    async with _client() as client:
        before = (await client.get("/tickets")).json()
        resp = await client.post(
            "/tickets",
            json={"title": "钉了个不存在的", "bug_report": BUG_REPORT, "workflow_id": "查无此流程"},
        )
        assert resp.status_code == 404
        assert (await client.get("/tickets")).json() == before


async def test_create_ticket_without_workflow_leaves_it_unpinned(stores) -> None:
    """不传 / 空串 / 空白串 → 一律当"没钉"（列 None），发起时退回「最新一条」兜底。

    向后兼容：现有的 `POST /tickets` 调用方不传这个字段，行为必须一字不变。
    空白串也要收进去 —— 模板里留个空值就变成"钉了一个空名字"、发起时 409，太脆。
    """
    async with _client() as client:
        for over in ({}, {"workflow_id": ""}, {"workflow_id": "   "}):
            t = await _create(client, **over)
            assert t["workflow_name"] is None, f"body={over} 不该钉住任何东西"


async def test_ticket_created_with_a_workflow_runs_that_one(stores) -> None:
    """**本功能的验收用例**：建单选了哪条 → 点「发起诊断」就跑哪条。

    "让 ticket 按选定的 workflow 执行"这句话的全部内容就是这条链：
    前端选 id → 建单时查回名字钉上 → 运行期按名字查回 id。
    任何一环断了（钉错、钉了 id、运行期没读它）这条都会红。
    """
    w = stores["workflow"]
    # created_at 显式给：靠 save() 的 now 撞运气的话"谁更新"本身就不确定
    await w.insert_if_absent(
        "w-old", "old-flow", _wf_yaml("old-flow"), "2026-01-01T00:00:00+00:00"
    )
    await w.insert_if_absent(
        "w-new", "new-flow", _wf_yaml("new-flow"), "2026-02-01T00:00:00+00:00"
    )

    async with _client() as client:
        # 刻意选**旧的**那条 —— 兜底本来会跑 w-new，选中的是 w-old，
        # 于是"跑了选中的"与"跑了最新的"在这条里可区分。
        t = await _create(client, workflow_id="w-old")
        assert t["workflow_name"] == "old-flow"
        out = (await client.post(f"/tickets/{t['id']}/run", json={})).json()
        assert out["workflow_id"] == "w-old"


async def test_run_ticket_uses_the_pinned_workflow_not_the_newest(stores) -> None:
    """工单钉了哪条就跑哪条 —— 这是「钉住」这件事的全部意义。

    改之前只有一条隐式规则：请求不带 `workflow_id` 就取 `list()` 首条（= 最新）。
    于是**推一条新流程，在途工单的目标就悄悄换了**（实测：`problem-diagnose-fix`
    一推进 otr 库，所有工单的发起诊断都改了道，而页面上看不出来）。
    """
    w = stores["workflow"]
    # created_at **显式给**：靠 save() 的 now 撞运气的话，"谁更新"本身就不确定，
    # 这条测试会时红时绿（`list()` 只有 created_at 一个排序键）。
    await w.insert_if_absent(
        "w-old", "old-flow", _wf_yaml("old-flow"), "2026-01-01T00:00:00+00:00"
    )
    await w.insert_if_absent(
        "w-new", "new-flow", _wf_yaml("new-flow"), "2026-02-01T00:00:00+00:00"
    )
    ticket_inputs = {"bug_report": BUG_REPORT}

    async with _client() as client:
        # ① 没钉（老工单 / 手工建的单）→ 仍走"最新那条"兜底，行为不变
        plain = await _create(client)
        out = (await client.post(f"/tickets/{plain['id']}/run", json={})).json()
        assert out["workflow_id"] == "w-new"

        # ② 钉了 old-flow → 跑 old-flow，**不是**最新的那条
        # （`create` 返回 id 字符串；返回行的是 `create_once`）
        pinned = await stores["ticket"].create(
            "local", title="钉过的单", inputs=ticket_inputs, workflow_name="old-flow"
        )
        out = (await client.post(f"/tickets/{pinned}/run", json={})).json()
        assert out["workflow_id"] == "w-old"

        # ③ body 显式指定压过钉子（人工覆盖）
        out = (
            await client.post(f"/tickets/{pinned}/run", json={"workflow_id": "w-new"})
        ).json()
        assert out["workflow_id"] == "w-new"


async def test_run_ticket_keeps_diagnosis_when_the_graph_reads_it(stores) -> None:
    """图**读**它就别剥：修复流程的 plan 就是靠工单里那份诊断拿根因与方案的。

    回归（run_12ebabdd80 实测）：`_run_inputs_from_ticket` 起初**无条件**剥掉
    `bug_report.diagnosis`（理由是"诊断是产物，回流会锚定模型"），而
    `problem-diagnose-fix` 恰恰把它当入参 —— 于是升级建出来的单点「发起」**必然挂在
    plan 的 require 上**，报「未满足 require ['rca', 'solution']」，而工单里明明有诊断。
    判据现在交给图自己（params 里引用了就留着），两条需求不再互相踩。
    """
    async with _client() as client:
        await client.post(
            "/workflows", json={"name": "fix-flow", "yaml": READS_DIAGNOSIS_YAML}
        )
        diag = {"rca": {"summary": "NPE"}, "plan": {"summary": "补判空"}}
        t = await stores["ticket"].create(
            "local",
            title="升级建出来的单",
            inputs={"bug_report": {**BUG_REPORT, "diagnosis": diag}},
        )
        out = (await client.post(f"/tickets/{t}/run", json={})).json()
        run = next(r for r in (await client.get("/runs")).json() if r["run_id"] == out["run_id"])
        assert run["inputs"]["bug_report"]["diagnosis"] == diag


async def test_run_ticket_409_when_pinned_workflow_is_gone(stores) -> None:
    """钉的流程在库里找不到（被删/改名）→ **409 且什么都不做**：不退回默认、不起 run。

    刻意 fail-closed：静默跑一条不是他要的流程比跑不起来危险得多，而"退回了默认"在
    页面上看不出来——只有 run 列表里的流程名对不上，那时已经跑完了。
    """
    async with _client() as client:
        await client.post("/workflows", json={"name": "whatever", "yaml": VALID_YAML})
        t = await stores["ticket"].create(
            "local",
            title="钉了个不存在的",
            inputs={"bug_report": BUG_REPORT},
            workflow_name="被删掉的流程",
        )
        resp = await client.post(f"/tickets/{t}/run", json={})
        assert resp.status_code == 409
        assert "被删掉的流程" in resp.json()["detail"]

        # 409 必须发生在 start_run **之前**：run 没起、工单没动
        assert (await client.get("/runs")).json() == []
        after = (await client.get(f"/tickets/{t}")).json()
        assert after["run_ids"] == []
        assert after["status"] == "new"


async def test_run_ticket_without_any_workflow_400(stores) -> None:
    async with _client() as client:
        t = await _create(client)
        resp = await client.post(f"/tickets/{t['id']}/run", json={})

    assert resp.status_code == 400
    assert "workflow" in resp.json()["detail"]


async def test_run_ticket_unknown_id_404(stores) -> None:
    async with _client() as client:
        resp = await client.post("/tickets/nope/run", json={})

    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────
# 幂等建单（`create_once`）：`kind: ticket` 节点的落点
# ──────────────────────────────────────────────────────────────────
async def test_create_once_returns_the_existing_ticket_for_same_source_ref(tmp_path) -> None:
    """同一 `source_ref` 建两次 → **只有一张**，第二次拿到的是同一张（`created=False`）。

    这是「一条数据永远只有一张工单」这个不变量的落点。它比引擎的
    `external_operation_id` 强：后者只活在 attempt 账本里，管得住"同 run 重放"，
    管不住"同一条数据被两个 run / 两次手工重跑各建一张"。
    """
    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    a, ca = await ts.create_once("otr", source_ref="PR-0001", title="t1", inputs={"x": 1})
    b, cb = await ts.create_once("otr", source_ref="PR-0001", title="t1-again", inputs={"x": 2})
    assert ca is True and cb is False
    assert a["id"] == b["id"]
    assert a["number"] == b["number"]          # 第二次没有另取号
    assert b["title"] == "t1"                  # 也没有被覆盖
    assert len(await ts.list("otr")) == 1


async def test_create_once_is_race_safe(tmp_path) -> None:
    """并发建同一条 → 仍只有一张（唯一索引 + DO NOTHING + 回读兜底）。"""
    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    results = await asyncio.gather(
        *[ts.create_once("otr", source_ref="PR-0002", title="c", inputs={}) for _ in range(8)]
    )
    assert len({row["id"] for row, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    assert len(await ts.list("otr")) == 1


async def test_create_once_is_scoped_per_tenant(tmp_path) -> None:
    """同一个 `source_ref` 在两个租户下是**两张**单（`source_ref` 是租户内的号）。"""
    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    a, ca = await ts.create_once("otr", source_ref="PR-0003", title="a", inputs={})
    b, cb = await ts.create_once("local", source_ref="PR-0003", title="b", inputs={})
    assert ca is True and cb is True and a["id"] != b["id"]


async def test_next_number_format_and_is_per_day(tmp_path) -> None:
    """工单号 `INC-YYYYMMDD-NNNN`，同日自增、跨日归 1。"""
    from datetime import UTC, datetime

    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    d1 = datetime(2026, 9, 21, tzinfo=UTC)
    d2 = datetime(2026, 9, 22, tzinfo=UTC)
    assert await ts.next_number(now=d1) == "INC-20260921-0001"
    assert await ts.next_number(now=d1) == "INC-20260921-0002"
    assert await ts.next_number(now=d2) == "INC-20260922-0001"


async def test_legacy_db_without_source_ref_gets_the_columns(tmp_path) -> None:
    """**已开通的租户库**（`tickets` 表早就在、缺后加的列）连上后要能补列。

    缺 `source_ref`：`CREATE UNIQUE INDEX` 会直接报 "no such column"。
    缺 `workflow_name`：读单时 `_COLS` 里带了它，SQL 直接报 no such column —— 老库整个
    工单页面挂掉。
    而 `CREATE TABLE IF NOT EXISTS` 对已存在的表什么也不做 —— 这是只在**升级已有环境**
    时才暴露的坑（本机的 otr / local 正是这种库）。
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE tickets (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, number TEXT,
           title TEXT NOT NULL, service TEXT, namespace TEXT, severity TEXT,
           status TEXT NOT NULL, inputs TEXT NOT NULL, run_ids TEXT NOT NULL,
           created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    c.execute(
        "INSERT INTO tickets VALUES ('old1','otr',NULL,'legacy',NULL,NULL,NULL,'new','{}','[]','t','t')"
    )
    c.commit()
    c.close()

    ts = TicketStore(path)
    await ts.connect()  # 不抛
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(tickets)")}
    assert "source_ref" in cols
    assert "workflow_name" in cols  # 后加的第二列，同样要补
    # 历史行（source_ref 为 NULL）不参与唯一约束，也不影响新单
    assert len(await ts.list("otr")) == 1
    await ts.create_once("otr", source_ref="PR-0009", title="new", inputs={})
    assert len(await ts.list("otr")) == 2


async def test_create_from_node_params_maps_bug_report_and_requires_source_ref(tmp_path) -> None:
    """`kind: ticket` 节点的 params → 工单字段；缺 `source_ref` 直接报错。"""
    from agentflow.api.ticket_store import create_from_node_params

    ts = TicketStore(tmp_path / "t.db")
    await ts.connect()
    params = {
        "source_ref": "PR-0100",
        "bug_report": {
            "number": "PR-0100",
            "short_description": "结账单打印无反应",
            "severity": "high",
            "cmdb_ci": {"name": "order-service", "namespace": "order"},
        },
        "window_start": "2026-09-21T07:00:00+00:00",
        "window_end": "2026-09-21T08:00:00+00:00",
        "rca": {"hypotheses": ["NPE"]},
        "plan": {"summary": "补空值校验"},
        "next_workflow": "problem-diagnose-fix",
    }
    out = await create_from_node_params(ts, "otr", params)
    assert out["created"] is True
    assert out["ticket_number"].startswith("INC-")
    row = await ts.get("otr", out["ticket_id"])
    assert row["title"] == "结账单打印无反应"
    assert row["service"] == "order-service"
    assert row["namespace"] == "order"
    assert row["severity"] == "high"
    assert row["inputs"]["bug_report"]["number"] == "PR-0100"
    # 诊断结论写在 **bug_report 里面** —— 工单详情页只渲染这一段（见
    # `_ticket_fields_from_params` 的注释：写在 inputs 顶层等于没人看得见）
    assert row["inputs"]["bug_report"]["diagnosis"]["plan"]["summary"] == "补空值校验"
    assert "diagnosis" not in row["inputs"]
    # 不能就地改调用方的 params（executor 交过来的那份是共用的形状）
    assert "diagnosis" not in params["bug_report"]

    # `next_workflow` → 工单的 `workflow_name`**列**（发起诊断时按它选流程）。
    # 它绝不能进 `inputs`：inputs 会被原样当成下一次 run 的 inputs 下发给 agent，
    # 平台内部指针进去就变成"喂给模型的东西"（诊断那次已经踩过一次）。
    assert row["workflow_name"] == "problem-diagnose-fix"
    assert "workflow_name" not in row["inputs"]
    assert "next_workflow" not in row["inputs"]["bug_report"]

    # 没声明 → 列为 None（发起诊断时退回"最新一条"兜底）
    out2 = await create_from_node_params(
        ts, "otr", {"source_ref": "PR-0101", "bug_report": {"number": "PR-0101"}}
    )
    assert (await ts.get("otr", out2["ticket_id"]))["workflow_name"] is None
    # 空白串也当没声明（模板里留空不该变成"钉了一个空名字"→ 发起诊断 409）
    out3 = await create_from_node_params(
        ts,
        "otr",
        {"source_ref": "PR-0102", "bug_report": {"number": "PR-0102"}, "next_workflow": "  "},
    )
    assert (await ts.get("otr", out3["ticket_id"]))["workflow_name"] is None

    # 缺 source_ref（幂等判据）→ 失败，而不是建一张无法去重的单
    with pytest.raises(ValueError, match="source_ref"):
        await create_from_node_params(ts, "otr", {"bug_report": {"number": "X"}})


async def test_run_ticket_aligns_the_dispatched_ticket_number(stores) -> None:
    """run 的入参里，`bug_report.number` 必须是**派单号**（工单自己的 number）。

    实测（run_a80df3e5d3）：工单 ``number=INC-20260922-0002``（派单号），而载荷
    ``bug_report.number=PR-20260922-0001``（问题单号，APM 侧用 ``rec["record_id"]`` 填的）。
    `ticket-done` 按提示词取的是后者 → 回传端点只认派单号 → **404「没有持有工单 PR-…」**、
    整条 run 红在最后一公里。"修复已完成、PR 也开了"，但原系统永远收不到。
    """
    async with _client() as client:
        await client.post("/workflows", json={"name": "fix-flow", "yaml": READS_DIAGNOSIS_YAML})
        other = {"bug_report": {**BUG_REPORT, "number": "PR-20260922-0001",
                                "diagnosis": {"rca": {"summary": "x"}}},
                 "number": "INC-20260922-0002"}
        t = await _create(client, **other)

        resp = await client.post(f"/tickets/{t['id']}/run")
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        run = next(r for r in (await client.get("/runs")).json() if r["run_id"] == run_id)

    bug = run["inputs"]["bug_report"]
    assert bug["number"] == "INC-20260922-0002", "回传要的是派单号，run 里就得是它"
    assert bug["problem_number"] == "PR-20260922-0001", "问题单号要留痕，别悄悄丢"
    # 工单自己那份记录**不能被动过**（详情页显示的仍是原样）
    stored = await stores["ticket"].get(t["tenant_id"], t["id"])
    assert stored["inputs"]["bug_report"]["number"] == "PR-20260922-0001"
    assert "problem_number" not in stored["inputs"]["bug_report"]


async def test_run_ticket_same_number_needs_no_problem_number(stores) -> None:
    """两个号**本来就一样**时（显式不传 → 建单接口从载荷兜底取）→ 什么都不改。

    这是对齐逻辑的负向对照：只在"工单号 ≠ 载荷号"时才改写并留痕，别给每张单都塞一个
    `problem_number`（那会把"两个号"这件事变成噪音，反而看不出哪里真的对不上）。
    """
    async with _client() as client:
        await client.post("/workflows", json={"name": "simple-flow", "yaml": VALID_YAML})
        t = await _create(client, number=None)  # 兜底取 bug_report.number
        assert t["number"] == BUG_REPORT["number"]

        resp = await client.post(f"/tickets/{t['id']}/run")
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        run = next(r for r in (await client.get("/runs")).json() if r["run_id"] == run_id)

    bug = run["inputs"]["bug_report"]
    assert bug["number"] == BUG_REPORT["number"]
    assert "problem_number" not in bug
