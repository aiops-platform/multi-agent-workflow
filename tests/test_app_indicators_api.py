"""``GET /app-indicators`` 路线测试（遗留前端 Smart Inspection 的数据面）。

与 test_cors.py 同款：``httpx.AsyncClient`` + ``ASGITransport``（不触发 lifespan），
用 ``monkeypatch`` 换掉 ``app.py`` 的模块全局 service。
"""
import httpx
import pytest

import agentflow.api.app as app_mod
from agentflow.api.app import app


class _StubService:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def snapshot(self) -> dict:
        return self.payload

    async def aclose(self) -> None:
        pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture
def _stub_ok(monkeypatch):
    payload = {
        "success": True,
        "data": {
            "content": [{"serviceId": 123456, "serviceName": "order-service",
                         "cpuUsage": 0.4, "memoryUsage": 22.1, "status": "HEALTHY",
                         "alertCount": None, "restartCount": None}],
            "page": 0, "size": 1, "total": 1, "totalPages": 1, "hasNext": False,
            "meta": {"source": "prometheus", "servicesDiscovered": 1, "podsLive": 1,
                     "warnings": []},
        },
        "error": None,
    }
    monkeypatch.setattr(app_mod, "app_indicators_service", _StubService(payload))
    return payload


async def test_endpoint_requires_no_authentication(_stub_ok) -> None:
    """**把「不鉴权」钉成回归测试。**

    调用方是未迁移的遗留页面，发的是不带任何头的裸 ``fetch``。一旦有人给这个路由加上
    ``Depends(get_tenant_context)``，Smart Inspection 会整页失效——而那是很容易
    "顺手统一一下"的改动，所以这里显式锁住。
    """
    async with _client() as client:
        resp = await client.get("/app-indicators")     # 刻意不带任何 Header / Token
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_endpoint_returns_frontend_envelope_shape(_stub_ok) -> None:
    """信封形状必须与遗留前端 ``data.success && data.data && data.data.content`` 对齐。"""
    async with _client() as client:
        body = (await client.get("/app-indicators")).json()
    assert set(body) == {"success", "data", "error"}
    assert set(body["data"]) >= {"content", "page", "size", "total", "totalPages", "hasNext"}
    row = body["data"]["content"][0]
    assert row["serviceId"] == 123456 and row["alertCount"] is None


async def test_upstream_failure_still_returns_http_200(monkeypatch) -> None:
    """数据面失败也必须 200：前端先判 ``if (!res.ok) throw`` 并丢弃 body，
    只有 200 才能把 error 文案送到页面错误条。"""
    payload = {"success": False, "data": None, "error": "应用指标后端不可用：Prometheus 不可达"}
    monkeypatch.setattr(app_mod, "app_indicators_service", _StubService(payload))
    async with _client() as client:
        resp = await client.get("/app-indicators")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False and body["data"] is None
    assert "不可用" in body["error"]


async def test_cors_headers_present(_stub_ok) -> None:
    """前端以相对路径 ``/agentflow`` 调用（同源），但直接跨域调也必须可用。"""
    async with _client() as client:
        resp = await client.get("/app-indicators", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in resp.headers}
