# -*- coding: utf-8 -*-
"""MCPClientManager 测试：真实拉起 stdio mock server 子进程验证连接/下发/allow 名单。

- test_connection：stdio 连上列 3 工具（只读/含`.` sanitize/非只读标注齐全）；http 连不上 → ok:false。
- load/clients_for_agent：server 侧无 agent 绑定（agents 字段已移除）→ 任意 agent 拿全部 enabled
  client；stateful connect（真实子进程）。
- allow_names_for_agent：返回 AgentScope 精确名 ``mcp__mock-mcp__...``（无 sanitize 漂移）。

子进程在 finally/close_all 杀掉，避免泄漏。
"""
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentflow.agents.mcp_manager import MCPClientManager
from agentflow.api.mcp_store import MCPStore

MOCK_PY = Path(__file__).resolve().parents[1] / "scripts" / "mock_mcp_server.py"


def _stdio_row(name: str = "mock-mcp", **over) -> dict:
    row = {
        "name": name,
        "transport": "stdio",
        "config": {"command": sys.executable, "args": [str(MOCK_PY)], "env": None, "cwd": None},
        "is_stateful": True,
        "enable_tools": None,
        "disable_tools": None,
        "enabled": True,
    }
    row.update(over)
    return row


def _http_row(url: str, **over) -> dict:
    row = {
        "name": "http-mock",
        "transport": "http",
        "config": {"url": url},
        "is_stateful": False,
        "enable_tools": None,
        "disable_tools": None,
        "enabled": True,
    }
    row.update(over)
    return row


async def test_test_connection_stdio_returns_three_tools() -> None:
    mgr = MCPClientManager(MCPStore(":memory:"))
    res = await mgr.test_connection(_stdio_row())
    assert res["ok"] is True, res.get("error")
    assert res["transport"] == "stdio"
    tools = {t["name"]: t for t in res["tools"]}
    assert set(tools) == {"get_weather", "query.repo", "send_alert"}
    # 只读标注：weather/repo 只读；send_alert 非只读（需 allow 规则）
    assert tools["get_weather"]["read_only"] is True
    assert tools["query.repo"]["read_only"] is True
    assert tools["send_alert"]["read_only"] is False
    # AgentScope LLM 侧精确名（含 sanitize：query.repo → queryxrepo）
    assert tools["query.repo"]["llm_name"] == "mcp__mock-mcp__queryxrepo"
    assert tools["get_weather"]["llm_name"] == "mcp__mock-mcp__get_weather"


async def test_test_connection_http_unreachable_ok_false() -> None:
    mgr = MCPClientManager(MCPStore(":memory:"))
    res = await mgr.test_connection(_http_row("http://127.0.0.1:9/mcp"))
    assert res["ok"] is False
    assert res["transport"] == "http"
    assert "error" in res


async def test_test_connection_http_http_error_ok_false() -> None:
    """回归：服务端可达但返回 HTTP 错误（如 401 鉴权失败）→ 收敛 {ok:false}，不得 500。

    曾踩：mcp streamable http client 把底层 401 经 anyio cancel scope 转成 CancelledError
    （BaseException），``except Exception`` 抓不住 → 测试连接直接 500。
    """
    class _AuthHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # initialize 等任意 POST → 401 JSON
            body = json.dumps({"error": "invalid_token", "error_description": "Authentication required"}).encode()
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("www-authenticate", 'Bearer error="invalid_token"')
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 静默健康检查/测试噪音
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AuthHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        mgr = MCPClientManager(MCPStore(":memory:"))
        res = await mgr.test_connection(
            _http_row(f"http://127.0.0.1:{port}/mcp", is_stateful=True, config={
                "url": f"http://127.0.0.1:{port}/mcp",
                "headers": {"Authorization": "bad-token"},
            })
        )
        assert res["ok"] is False  # 不抛异常（CancelledError/跨任务 cancel scope）→ 500
        assert res["transport"] == "http"
        assert "error" in res
        assert res["tools"] == []
    finally:
        srv.shutdown()
        srv.server_close()


async def test_test_connection_bad_config_ok_false() -> None:
    mgr = MCPClientManager(MCPStore(":memory:"))
    row = {"name": "bad", "transport": "stdio", "config": {"command": ""}, "is_stateful": True}
    res = await mgr.test_connection(row)
    assert res["ok"] is False
    assert "error" in res  # 建 client 或连接失败都收敛为 {ok:false, error}


async def test_load_clients_for_agent_returns_all_enabled() -> None:
    """server 侧无 agent 绑定（agents 字段已移除）：每个 agent 都拿到全部 enabled client。"""
    store = MCPStore(":memory:")
    mgr = MCPClientManager(store)
    await store.save(_stdio_row(name="mock-mcp"))
    await store.save(_stdio_row(name="http-only", transport="http", is_stateful=False,
                                config={"url": "http://127.0.0.1:9/mcp"}))
    await mgr.load()  # mock-mcp(stateful)→connect；http-only(stateless) 无需连接
    try:
        for agent in ("triage", "log-analyst", "whatever"):  # 任意 agent 拿到全部 enabled
            clients = await mgr.clients_for_agent(agent)
            assert {c.name for c in clients} == {"mock-mcp", "http-only"}
            assert all(c.is_connected for c in clients if c.is_stateful)
    finally:
        await mgr.close_all()


async def test_allow_names_for_agent_returns_exact_llm_names() -> None:
    store = MCPStore(":memory:")
    mgr = MCPClientManager(store)
    await store.save(_stdio_row())
    await mgr.load()
    try:
        # 无绑定 → 任意 agent 名都拿到全部 enabled client 的工具名
        for agent in ("triage", "nobody"):
            names = await mgr.allow_names_for_agent(agent)
            assert set(names) == {
                "mcp__mock-mcp__get_weather",
                "mcp__mock-mcp__queryxrepo",  # query.repo sanitize 后
                "mcp__mock-mcp__send_alert",
            }
    finally:
        await mgr.close_all()


async def test_refresh_server_rebuilds_and_evicts() -> None:
    store = MCPStore(":memory:")
    mgr = MCPClientManager(store)
    mid = await store.save(_stdio_row())
    await mgr.refresh_server(mid)  # 建 + connect
    try:
        assert [c.name for c in await mgr.clients_for_agent("triage")] == ["mock-mcp"]
    finally:
        await mgr.close_all()
    # enabled=0 → refresh 只 evict（不再有 client）
    await store.update(mid, _stdio_row(enabled=False))
    await mgr.refresh_server(mid)
    assert await mgr.clients_for_agent("triage") == []
    # store 中删除 → refresh 幂等 evict
    await store.delete(mid)
    await mgr.refresh_server(mid)
    assert await mgr.clients_for_agent("triage") == []


@pytest.mark.skipif(sys.platform.startswith("win"), reason="子进程/信号仅类 Unix 验证")
async def test_stateless_http_live_streamable() -> None:
    """端到端验证 streamable HTTP（stateless）：起 mock server 子进程 → test_connection 列 3 工具。"""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_PY), "--http", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    mgr = MCPClientManager(MCPStore(":memory:"))
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait_tcp(port)  # 先等 uvicorn 端口 accept，避免连接拒绝的 TaskGroup 噪音
        res = {"ok": False}
        for _ in range(10):
            res = await mgr.test_connection(_http_row(url))
            if res["ok"]:
                break
        assert res["ok"] is True, f"http mock 未就绪: {res.get('error')}"
        assert {t["name"] for t in res["tools"]} == {"get_weather", "query.repo", "send_alert"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_tcp(port: int, tries: int = 40) -> None:
    """轮询等 TCP 端口可连接（uvicorn 起来前 connection refused）。"""
    import asyncio

    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.2)
    raise AssertionError(f"端口 {port} 未在预期时间内 accept")
