"""node_traces 全链路测试：采集 → 落库 → API 读取。

覆盖四条路径（对齐 transcript.py / executor._flush_node_trace / app.get_run_traces）：
1. 采集原语：server_from_tool_name 还原 MCP server；scan_denied_blocks 按 block.id
   把 ToolCallBlock 与 DENY 的 ToolResultBlock 配对（含真实 DONT_ASK 拒绝路径）。
2. 真实 agent 路径：TraceRecorder 经 AgentScope middleware 记下 llm_call / tool_call；
   AgentNodeRunner 汇总 kind='node' 行（输入/输出/tokens/cost/llm_steps/tool_steps）。
3. store.replace_node_traces 先删后插语义：retry/resume 只留末次成功 attempt 全量流水
   （memory + sqlite 双端一致）+ get 过滤。
4. executor flush → 派生审计（tool_call→ALLOW / denied→DENY）→ app GET /runs/{id}/traces 端到端。
"""
import json

import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.agents.mcp import build_toolkit
from agentflow.agents.runner import AgentNodeRunner, UsageTrackingModel
from agentflow.agents.scopes import ScriptedJsonModel, build_agent, run_agent
from agentflow.agents.transcript import (
    K_DENIED,
    K_LLM_CALL,
    K_NODE,
    K_TOOL_CALL,
    TraceRecorder,
    scan_denied_blocks,
    server_from_tool_name,
)
from agentflow.api.app import app
from agentflow.api.workflow_store import WorkflowStore
from agentflow.core.dag import Node
from agentflow.core.workflow import Workflow
from agentflow.service import RunService
from agentflow.statestore.memory import InMemoryStateStore
from agentflow.statestore.sqlite import SqliteStateStore

TRACE_YAML = """
name: trace-flow
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug_report" } }
  rca:    { agent: root-cause, params: { code: "$.nodes.triage.output.summary" } }
edges:
  - { from: triage, to: rca }
"""


# ======================================================================
# 确定性 mock 模型
# ======================================================================
def _one(content_block):
    from agentscope.message import TextBlock, ToolCallBlock  # noqa: F401
    from agentscope.model import ChatResponse

    async def _gen():
        yield ChatResponse(content=[content_block], is_last=True)

    return _gen()


class _FirstToolModel(ScriptedJsonModel):
    """第 1 次喂入工具请求 search_knowledge（本地工具 → 真实走 on_acting），之后输出 JSON。

    数据源工具（get_trace 等）已迁 MCP（design-v5.5），不在本地 toolkit。
    """

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        from agentscope.message import TextBlock, ToolCallBlock

        self._call_count += 1
        if self._call_count == 1 and tools:
            return _one(ToolCallBlock(id="c1", name="search_knowledge", input='{"query": "磁盘写满"}'))
        return _one(TextBlock(text=json.dumps(self._output_json, ensure_ascii=False)))


# ======================================================================
# 采集原语单测
# ======================================================================
def test_server_from_tool_name_maps_mcp_prefix() -> None:
    assert server_from_tool_name("mcp__git-srv__commit_working") == "git-srv"
    assert server_from_tool_name("mcp__my-server__queryxrepo") == "my-server"
    # 非 MCP function tool / 畸形名 / 空 → None
    assert server_from_tool_name("query_logs") is None
    assert server_from_tool_name("mcp__only-two-segs") is None
    assert server_from_tool_name("") is None
    assert server_from_tool_name(None) is None


def test_scan_denied_pairs_toolcall_by_id_and_extracts_server() -> None:
    """合成 context：ToolCallBlock + 同 id 的 DENY ToolResultBlock 跨消息配对。"""
    from agentscope.message import AssistantMsg, ToolCallBlock, ToolResultBlock, ToolResultState

    ctx = [
        AssistantMsg(name="triage", content=[
            ToolCallBlock(id="c1", name="get_trace", input='{"trace_id": "abc"}'),
        ]),
        AssistantMsg(name="triage", content=[
            ToolResultBlock(
                id="c1", name="get_trace",
                output="Permission denied for get_trace", state=ToolResultState.DENIED,
            ),
        ]),
        AssistantMsg(name="triage", content=[
            ToolCallBlock(id="c2", name="mcp__git-srv__commit_working", input='{"msg": "hi"}'),
            ToolResultBlock(
                id="c2", name="mcp__git-srv__commit_working",
                output="deny: tenant rule", state=ToolResultState.DENIED,
            ),
        ]),
    ]
    rows = scan_denied_blocks(ctx)
    assert all(r["kind"] == K_DENIED for r in rows)
    by_name = {r["name"]: r["payload"] for r in rows}
    assert by_name["get_trace"]["input"] == {"trace_id": "abc"}
    assert by_name["get_trace"]["server"] is None
    assert by_name["mcp__git-srv__commit_working"]["server"] == "git-srv"
    assert by_name["mcp__git-srv__commit_working"]["reason"].startswith("deny:")


async def test_scan_denied_catches_real_deny() -> None:
    """真实 DONT_ASK + 空 allow 规则 → toolkit 里的本地工具被 DENY，跑后能补扫配对。"""
    from agentscope.permission import PermissionContext, PermissionMode

    toolkit = build_toolkit("knowledge-lookup")
    model = UsageTrackingModel(_FirstToolModel({"ok": True}))
    recorder = TraceRecorder("knowledge-lookup", "knowledge-lookup")
    agent = build_agent(
        "knowledge-lookup", toolkit, model,
        permission_context=PermissionContext(mode=PermissionMode.DONT_ASK),
        middlewares=[recorder],
    )
    await run_agent(agent, {"bug": {"title": "x"}})
    # DENY 不进 on_acting → recorder 只记 llm_call；denied 由补扫获得
    assert all(r["kind"] != K_TOOL_CALL for r in recorder.rows)
    denied = scan_denied_blocks(agent.state.context)
    hit = [r for r in denied if r["name"] == "search_knowledge"]
    assert hit and hit[0]["payload"]["input"] == {"query": "磁盘写满"}
    assert "Permission denied" in hit[0]["payload"]["reason"]


# ======================================================================
# 真实 agent 采集（middleware 记 llm_call/tool_call + node 汇总）
# ======================================================================
async def test_runner_records_allowed_tool_and_node_summary() -> None:
    runner = AgentNodeRunner(_FirstToolModel({"summary": "ok"}))
    node = Node(id="n1", agent="knowledge-lookup")
    await runner(node, {"bug": {"title": "x"}})
    rows = runner.take_trace(node)
    assert rows and rows[0]["kind"] == K_NODE  # 汇总行在最前
    kinds = [r["kind"] for r in rows]
    assert K_TOOL_CALL in kinds and K_LLM_CALL in kinds

    node_row = rows[0]["payload"]
    assert node_row["agent"] == "knowledge-lookup"
    assert node_row["input"] == {"bug": {"title": "x"}}
    assert node_row["output"] == {"summary": "ok"}
    assert node_row["tool_steps"] == 1 and node_row["llm_steps"] >= 1
    assert {"tokens", "cost", "enabled"} <= set(node_row)

    tool = next(r for r in rows if r["kind"] == K_TOOL_CALL)
    assert tool["name"] == "search_knowledge"
    p = tool["payload"]
    assert p["server"] is None and p["is_mcp"] is False  # 本地工具（非 MCP）
    assert p["input"] == {"query": "磁盘写满"}
    assert p["result_state"] == "success"

    llm = next(r for r in rows if r["kind"] == K_LLM_CALL)
    assert {"messages", "tools", "usage"} <= set(llm["payload"])
    # knowledge-lookup 的本地工具：仅知识检索（数据查询与 CMDB 均已迁 MCP）
    assert llm["payload"]["tools"] == ["search_knowledge"]


# ======================================================================
# transcript 推理采集：ThinkingBlock → llm_call payload.reasoning（非流/流式）
# ======================================================================
async def test_transcript_captures_thinking_nonstream() -> None:
    """非流式响应含 ThinkingBlock → llm_call 行 payload.reasoning = 推理全文。"""
    from agentscope.message import TextBlock, ThinkingBlock
    from agentscope.model import ChatResponse

    recorder = TraceRecorder("root-cause", "root-cause-analyst")

    async def handler():
        return ChatResponse(
            content=[
                ThinkingBlock(thinking="先核对 log 与 code 证据…"),
                TextBlock(text='{"confidence": 0.3}'),
            ],
            is_last=True,
        )

    await recorder.on_model_call("root-cause-analyst", {"messages": []}, handler)
    llm = recorder.rows[0]
    assert llm["kind"] == K_LLM_CALL
    assert llm["payload"]["reasoning"] == "先核对 log 与 code 证据…"
    # 请求 messages 仍记、tools 缺省为空
    assert "messages" in llm["payload"] and llm["payload"]["tools"] == []


async def test_transcript_no_thinking_omits_reasoning_key() -> None:
    """响应只有 TextBlock（无 thinking）→ payload 不写 reasoning 键（无空串噪音）。"""
    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse

    recorder = TraceRecorder("triage", "triage")

    async def handler():
        return ChatResponse(content=[TextBlock(text='{"ok": true}')], is_last=True)

    await recorder.on_model_call("triage", {"messages": []}, handler)
    assert "reasoning" not in recorder.rows[0]["payload"]


async def test_transcript_accumulates_thinking_across_stream_chunks() -> None:
    """流式逐 chunk 增量 ThinkingBlock → 走完流后 payload.reasoning = 全段拼接。

    末 chunk（is_last=True）按真实 DeepSeek 形状会在 content 里**重发**整段已累积 thinking
    → 采集跳过末 chunk，避免与前面增量拼接重复（double-count）。
    """
    from agentscope.message import TextBlock, ThinkingBlock
    from agentscope.model import ChatResponse

    recorder = TraceRecorder("root-cause", "root-cause-analyst")

    def _stream():
        async def _gen():
            yield ChatResponse(content=[ThinkingBlock(thinking="第一段思考")], is_last=False)
            yield ChatResponse(content=[ThinkingBlock(thinking="第二段思考")], is_last=False)
            # 末 chunk 重发全量 thinking + 文本（对齐 DeepSeek _model 末 chunk 形状）
            yield ChatResponse(
                content=[
                    ThinkingBlock(thinking="第一段思考第二段思考"),
                    TextBlock(text='{"c": 1}'),
                ],
                is_last=True,
            )

        return _gen()

    async def handler():
        return _stream()

    gen = await recorder.on_model_call("root-cause-analyst", {"messages": []}, handler)
    async for _ in gen:  # 等流走完才落行（finally 里 _add_llm）
        pass
    assert len(recorder.rows) == 1
    assert recorder.rows[0]["kind"] == K_LLM_CALL
    assert recorder.rows[0]["payload"]["reasoning"] == "第一段思考第二段思考"  # 拼接一次，无重复


# ======================================================================
# store.replace_node_traces：先删后插 → retry/resume 只留末次成功 attempt
# ======================================================================
@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStateStore()
    s = SqliteStateStore(tmp_path / "traces.db")
    await s.connect()
    return s


async def test_replace_node_traces_keeps_only_last_attempt(store) -> None:
    attempt1 = [
        {"kind": K_NODE, "name": None, "payload": {"attempt": 1}},
        {"kind": K_LLM_CALL, "name": "m", "payload": {"round": "a"}},
    ]
    attempt2 = [
        {"kind": K_NODE, "name": None, "payload": {"attempt": 2}},
        {"kind": K_TOOL_CALL, "name": "get_trace", "payload": {"input": {"trace_id": "x"}}},
        {"kind": K_LLM_CALL, "name": "m", "payload": {"round": "b"}},
    ]
    await store.replace_node_traces("r1", "n1", "t", rows=attempt1)
    await store.replace_node_traces("r1", "n1", "t", rows=attempt2)
    got = await store.get_node_traces("r1")
    assert len(got) == len(attempt2)  # attempt1 全被清掉
    assert [r["seq"] for r in got] == [0, 1, 2]  # seq 按 0 起重算
    assert [r["kind"] for r in got] == [K_NODE, K_TOOL_CALL, K_LLM_CALL]
    assert got[0]["payload"] == {"attempt": 2}

    # 不同节点互不影响（整节点替换只动 (run_id, node_id)）
    await store.replace_node_traces("r1", "n2", "t", rows=attempt1)
    got = await store.get_node_traces("r1")
    assert {r["node_id"] for r in got} == {"n1", "n2"}


async def test_get_node_traces_filter(store) -> None:
    await store.replace_node_traces("r1", "n1", "t", rows=[
        {"kind": K_NODE, "name": None, "payload": {}},
        {"kind": K_TOOL_CALL, "name": "get_trace", "payload": {"input": {}}},
        {"kind": K_LLM_CALL, "name": "m", "payload": {}},
    ])
    await store.replace_node_traces("r1", "n2", "t", rows=[
        {"kind": K_NODE, "name": None, "payload": {}},
    ])
    n1 = await store.get_node_traces("r1", node_id="n1")
    assert len(n1) == 3 and all(r["node_id"] == "n1" for r in n1)
    nodes = await store.get_node_traces("r1", kind=K_NODE)
    assert len(nodes) == 2 and all(r["kind"] == K_NODE for r in nodes)
    assert await store.get_node_traces("rX") == []


# ======================================================================
# executor flush：真实 trace 行落库 + 派生审计（tool_call→ALLOW / denied→DENY）
# ======================================================================
class _TraceFakeRunner:
    """无 LLM 的假 node_runner：每节点成功 + 脚本化 trace 行（含 tool_call 与 denied）。"""

    def __init__(self) -> None:
        self._rows = [
            {"kind": K_NODE, "name": None, "payload": {"agent": "triage", "output": {"ok": True}}},
            {"kind": K_TOOL_CALL, "name": "get_trace", "payload": {"input": {"trace_id": "abc"}}},
            {"kind": K_DENIED, "name": "mcp__git-srv__commit_working", "payload": {"input": {"msg": "x"}}},
        ]

    async def __call__(self, node: Node, params: dict) -> dict:
        return {"ok": True}

    def take_trace(self, node: Node) -> list[dict]:
        return json.loads(json.dumps(self._rows))  # 深拷贝防跨断言互扰


async def test_executor_flush_node_trace_derives_audit() -> None:
    store = InMemoryStateStore()
    wf = Workflow.load_yaml("name: t\nnodes:\n  n1: { agent: triage }\n")
    svc = RunService(store, node_runner=_TraceFakeRunner())
    summary = await svc.create_run("local", wf, inputs={})
    run_id = summary["run_id"]

    traces = await store.get_node_traces(run_id)
    assert [r["kind"] for r in traces] == [K_NODE, K_TOOL_CALL, K_DENIED]
    assert [r["seq"] for r in traces] == [0, 1, 2]

    logs = await store.get_audit_logs(run_id=run_id)
    decision = {l["tool_name"]: l["decision"] for l in logs}
    assert decision.get("get_trace") == "ALLOW"
    assert decision.get("mcp__git-srv__commit_working") == "DENY"
    assert all(l["actor"] == "triage" for l in logs)
    assert all(l["input_masked"] is not None for l in logs)  # 输入脱敏后落审计


# ======================================================================
# API 端到端：GET /runs/{id}/traces
# ======================================================================
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    ws = WorkflowStore(tmp_path / "wf.db")
    monkeypatch.setattr(app_mod, "workflow_store", ws)
    store = SqliteStateStore(tmp_path / "run.db")
    await store.connect()
    service = RunService(store)
    monkeypatch.setattr(app_mod, "service", service)
    return service


async def _save_workflow(client, yaml_text: str, name: str = "trace-flow") -> str:
    resp = await client.post("/workflows", json={"name": name, "yaml": yaml_text})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _wait_success(client, run_id: str, timeout: float = 8.0) -> dict:
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while True:
        data = (await client.get(f"/runs/{run_id}")).json()
        if data["status"] == "success":
            return data
        assert time.monotonic() < deadline, f"run {run_id} 未在 {timeout}s 内 success: {data['status']}"
        await asyncio.sleep(0.03)


async def test_api_get_run_traces_end_to_end(svc) -> None:
    svc.node_runner = AgentNodeRunner(ScriptedJsonModel({"summary": "diagnosed"}))
    async with _client() as client:
        wid = await _save_workflow(client, TRACE_YAML)
        run_id = (await client.post("/run", json={"workflow_id": wid, "ticket": {"bug_report": {"title": "x"}}})).json()["run_id"]
        await _wait_success(client, run_id)

        resp = await client.get(f"/runs/{run_id}/traces")
        assert resp.status_code == 200
        rows = resp.json()
        node_ids = {r["node_id"] for r in rows if r["kind"] == K_NODE}
        assert node_ids == {"triage", "rca"}

        # node 汇总行的 agent 与 YAML 定义一致
        agent = {r["node_id"]: r["payload"]["agent"] for r in rows if r["kind"] == K_NODE}
        assert agent == {"triage": "triage", "rca": "root-cause"}

        # 每节点至少 1 次 llm_call，且 seq 从 0 连续
        for nid in ("triage", "rca"):
            nid_rows = [r for r in rows if r["node_id"] == nid]
            assert any(r["kind"] == K_LLM_CALL for r in nid_rows)
            assert [r["seq"] for r in nid_rows] == list(range(len(nid_rows)))

        # kind / node_id 过滤参数
        llm = (await client.get(f"/runs/{run_id}/traces", params={"kind": K_LLM_CALL})).json()
        assert llm and all(r["kind"] == K_LLM_CALL for r in llm)
        only_rca = (await client.get(f"/runs/{run_id}/traces", params={"node_id": "rca"})).json()
        assert only_rca and all(r["node_id"] == "rca" for r in only_rca)

    # 未知 run → 404
    async with _client() as client:
        assert (await client.get("/runs/does-not-exist/traces")).status_code == 404
