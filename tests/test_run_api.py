"""Run API「UI 兼容层」测试：异步 POST /run + 聚合 GET /runs/{id} + approve/reject/stop。

与 test_workflow_api.py / test_cors.py 同款：httpx.AsyncClient + ASGITransport（不触发 lifespan）。
通过 monkeypatch 把 app.workflow_store（tmp WorkflowStore）与 app.service
（RunService + tmp SqliteStateStore）替换为临时实例，避免污染 data/agentflow.db。
"""
import asyncio
import json
import time

import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.agents.scopes import ScriptedJsonModel
from agentflow.api.app import app
from agentflow.api.workflow_store import WorkflowStore
from agentflow.service import RunService
from agentflow.statestore.sqlite import SqliteStateStore

VALID_YAML = """
name: simple-flow
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug_report" } }
  rca:    { agent: root-cause, params: { code: "$.nodes.triage.output.summary" } }
edges:
  - { from: triage, to: rca }
"""

# 含审批节点：审批通过 → commit；审批拒绝 → commit 因 when=false 被 skip
APPROVAL_YAML = """
name: approval-flow
nodes:
  triage:
    agent: triage
  approve-changes:
    kind: approval
    approvers: [lead-engineer]
    timeout: 3600
    params: { summary: "$.nodes.triage.output.summary" }
  commit:
    agent: committer
    params: { summary: "$.nodes.approve-changes.output.status" }
edges:
  - { from: triage, to: approve-changes }
  - { from: approve-changes, to: commit, when: "$.nodes.approve-changes.output.approved == true" }
"""


def _client() -> httpx.AsyncClient:
    # base_url 必须给：httpx 用相对 URL 时 cookie 解析需要绝对 URL（ASGITransport 场景）
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    """替换 app.workflow_store（tmp WorkflowStore）+ app.service（RunService + tmp SqliteStateStore）。"""
    ws = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", ws)
    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    service = RunService(store)
    monkeypatch.setattr(app_mod, "service", service)
    return service


async def _save_workflow(client, yaml_text: str, name: str = "test-flow") -> str:
    resp = await client.post("/workflows", json={"name": name, "yaml": yaml_text})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _wait_status(client, run_id: str, target: set[str], timeout: float = 5.0) -> dict:
    """轮询 GET /runs/{id} 直到 status 进入 target 集合；返回最后一次 run dict。"""
    deadline = time.monotonic() + timeout
    while True:
        resp = await client.get(f"/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in target:
            return data
        if time.monotonic() > deadline:
            raise AssertionError(
                f"run {run_id} 未在 {timeout}s 内达到 {target}，当前 status={data['status']}"
            )
        await asyncio.sleep(0.03)


async def test_run_by_workflow_id_returns_started_then_success(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML)
        resp = await client.post(
            "/run", json={"workflow_id": wid, "ticket": {"bug_report": {"title": "x"}}}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    run_id = data["run_id"]

    async with _client() as client:
        run = await _wait_status(client, run_id, {"success"})
        assert run["run_id"] == run_id
        assert run["workflow"] == "simple-flow"
        assert run["graph"]["name"] == "simple-flow"
        assert {n["id"] for n in run["graph"]["nodes"]} == {"triage", "rca"}
        assert run["graph"]["edges"] == [{"from": "triage", "to": "rca", "when": None}]
        assert set(run["nodes"]) == {"triage", "rca"}
        assert run["nodes"]["triage"]["status"] == "done"
        assert run["nodes"]["triage"]["output"]["node"] == "triage"
        # mock 无 LLM → token/cost 诚实为 0
        assert run["total_tokens"] == 0
        assert run["total_cost"] == 0.0
        assert run["pending_approvals"] == []


async def test_run_by_workflow_yaml_compat(svc) -> None:
    async with _client() as client:
        resp = await client.post(
            "/run", json={"workflow_yaml": VALID_YAML, "inputs": {"bug_report": {}}}
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    run_id = resp.json()["run_id"]
    async with _client() as client:
        run = await _wait_status(client, run_id, {"success"})
        assert run["status"] == "success"


async def test_run_unknown_workflow_id_404(svc) -> None:
    async with _client() as client:
        resp = await client.post("/run", json={"workflow_id": "nope"})
    assert resp.status_code == 404


async def test_run_missing_source_400(svc) -> None:
    async with _client() as client:
        resp = await client.post("/run", json={})
    assert resp.status_code == 400


async def test_get_run_not_found_404(svc) -> None:
    async with _client() as client:
        resp = await client.get("/runs/nope")
    assert resp.status_code == 404


async def test_approval_pending_then_approve(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML, name="approval-flow")
        resp = await client.post("/run", json={"workflow_id": wid})
        run_id = resp.json()["run_id"]

        run = await _wait_status(client, run_id, {"waiting_approval"})
        assert len(run["pending_approvals"]) == 1
        p = run["pending_approvals"][0]
        assert p["node_id"] == "approve-changes"
        assert p["trigger"] is None
        assert p["upstream"]["triage"] is not None  # 上游输出

        resp2 = await client.post(
            f"/runs/{run_id}/approve", json={"node_id": "approve-changes"}
        )
        assert resp2.status_code == 200
        run2 = await _wait_status(client, run_id, {"success"})
        assert run2["nodes"]["approve-changes"]["status"] == "done"
        assert run2["nodes"]["commit"]["status"] == "done"


async def test_reject_approval_skips_downstream(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML, name="approval-flow")
        resp = await client.post("/run", json={"workflow_id": wid})
        run_id = resp.json()["run_id"]
        await _wait_status(client, run_id, {"waiting_approval"})

        resp2 = await client.post(
            f"/runs/{run_id}/reject", json={"node_id": "approve-changes", "comment": "no"}
        )
        assert resp2.status_code == 200
        run2 = await _wait_status(client, run_id, {"success"})
        assert run2["nodes"]["approve-changes"]["status"] == "rejected"
        # 拒绝 → when 边 INACTIVE → commit SKIPPED（终态）
        assert run2["nodes"]["commit"]["status"] == "skipped"


async def test_approve_query_param_compat(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML, name="approval-flow")
        resp = await client.post("/run", json={"workflow_id": wid})
        run_id = resp.json()["run_id"]
        await _wait_status(client, run_id, {"waiting_approval"})
        resp2 = await client.post(f"/runs/{run_id}/approve?node_id=approve-changes")
        assert resp2.status_code == 200
        run2 = await _wait_status(client, run_id, {"success"})
        assert run2["nodes"]["commit"]["status"] == "done"


async def test_stop_run_marks_cancelled(svc) -> None:
    async with _client() as client:
        wid = await _save_workflow(client, APPROVAL_YAML, name="approval-flow")
        resp = await client.post("/run", json={"workflow_id": wid})
        run_id = resp.json()["run_id"]
        # 等到 waiting_approval（后台任务已返回，executor 留在内存）后 stop → cancelled
        await _wait_status(client, run_id, {"waiting_approval"})
        resp2 = await client.post(f"/runs/{run_id}/stop")
        assert resp2.status_code == 200
        run = (await client.get(f"/runs/{run_id}")).json()
        assert run["status"] == "cancelled"
        # 待审批节点状态被置为 cancelled
        assert run["nodes"]["approve-changes"]["status"] == "cancelled"


async def test_stop_run_not_found_404(svc) -> None:
    async with _client() as client:
        resp = await client.post("/runs/nope/stop")
    assert resp.status_code == 404


class _UsageStubModel(ScriptedJsonModel):
    """确定性模型 + 每次调用携带 ChatUsage（模拟真实 LLM 的 usage 计量，供计量测试）。"""

    def __init__(self, output_json: dict, input_tokens: int = 100, output_tokens: int = 50) -> None:
        super().__init__(output_json)
        self._in_tokens = input_tokens
        self._out_tokens = output_tokens

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self._call_count += 1
        from agentscope.message import TextBlock
        from agentscope.model import ChatResponse, ChatUsage

        return ChatResponse(
            content=[TextBlock(text=json.dumps(self._output_json, ensure_ascii=False))],
            is_last=True,
            usage=ChatUsage(
                input_tokens=self._in_tokens, output_tokens=self._out_tokens, time=0.0
            ),
        )


async def test_usage_metering_flows_to_aggregate(svc) -> None:
    """真实 token/cost 计量链路：AgentNodeRunner 的 UsageTrackingModel 累加 ChatUsage
    → DAGExecutor 写节点 checkpoint → 聚合 GET /runs/{id} 返回非零 total_tokens/cost。"""
    from agentflow.agents.runner import AgentNodeRunner

    svc.node_runner = AgentNodeRunner(
        _UsageStubModel({"summary": "diagnosed", "status": "ok"}, input_tokens=100, output_tokens=50)
    )
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML)
        resp = await client.post(
            "/run", json={"workflow_id": wid, "ticket": {"bug_report": {"title": "x"}}}
        )
        run_id = resp.json()["run_id"]
        run = await _wait_status(client, run_id, {"success"})
        assert run["total_tokens"] > 0
        assert run["total_cost"] > 0.0
        assert run["nodes"]["triage"]["tokens"] > 0
        assert run["nodes"]["triage"]["cost"] > 0.0
        # 输出仍是 agent 生成（而非 mock 的 {"node":"triage","ok":true}）
        assert run["nodes"]["triage"]["output"]["summary"] == "diagnosed"


async def test_agent_node_runner_scripted(svc) -> None:
    """有 DEEPSEEK_API_KEY 时注入 AgentNodeRunner 的 wiring：节点输出来自 agent。

    用 ScriptedJsonModel（无 Key 的确定性回退）验证真实 runner 路径——输出是 agent
    生成的（``{"summary": "diagnosed"}``），而非 mock _default_runner 的
    ``{"node": id, "ok": true}``。
    """
    from agentflow.agents.runner import AgentNodeRunner
    from agentflow.agents.scopes import ScriptedJsonModel

    svc.node_runner = AgentNodeRunner(
        ScriptedJsonModel({"summary": "diagnosed", "status": "ok"})
    )
    async with _client() as client:
        wid = await _save_workflow(client, VALID_YAML)
        resp = await client.post(
            "/run", json={"workflow_id": wid, "ticket": {"bug_report": {"title": "x"}}}
        )
        run_id = resp.json()["run_id"]
        run = await _wait_status(client, run_id, {"success"})
        # triage 输出来自 agent（ScriptedJsonModel），而非 mock 的 {"node":"triage","ok":true}
        assert run["nodes"]["triage"]["output"]["summary"] == "diagnosed"
        assert run["nodes"]["triage"]["output"].get("node") != "triage"
        assert run["nodes"]["rca"]["status"] == "done"
