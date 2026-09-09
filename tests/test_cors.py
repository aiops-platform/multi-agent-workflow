# -*- coding: utf-8 -*-
"""CORS 测试：控制面 API 允许前端跨域调用（默认 allow_origins=*）。

与 test_agents_api.py 同款：httpx.AsyncClient + ASGITransport（不触发 lifespan）。
CORSMiddleware 在 ASGI 层生效，与传输方式无关。
"""
import httpx

from agentflow.api.app import app

FRONTEND_ORIGIN = "http://localhost:3000"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_preflight_options_returns_cors_headers() -> None:
    """预检（Preflight）：跨域带自定义头时 OPTIONS 应返回 Allow-Origin。"""
    async with _client() as client:
        resp = await client.options(
            "/agents",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


async def test_simple_get_returns_allow_origin() -> None:
    """简单 GET：带 Origin 的响应头应含 Access-Control-Allow-Origin。"""
    async with _client() as client:
        resp = await client.get("/agents", headers={"Origin": FRONTEND_ORIGIN})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
