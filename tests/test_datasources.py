# -*- coding: utf-8 -*-
"""数据源适配器单测（mock httpx transport，不依赖真实 testbed）。

- ``_promql``：PromQL 表达式生成
- query_logs / query_metrics：请求构造 + 解析（MockTransport）
- build_l1_tools 数据源绑定：工具签名不变，实现指向真实 adapter
（真实 testbed 联调验证在会话记录中完成：ES/Prometheus/kubectl 三源均已实测）
"""
from __future__ import annotations

import httpx
import pytest

from agentflow.agents.datasources import RealDataSourceAdapter, _promql


# ----------------------------------------------------------------------
# PromQL 表达式
# ----------------------------------------------------------------------
def test_promql_by_metric() -> None:
    cpu = _promql("cpu", "order-service.*")
    assert "container_cpu_usage_seconds_total" in cpu
    assert 'pod=~"order-service.*"' in cpu
    disk = _promql("disk", "warranty-service.*")
    assert "container_fs_usage_bytes" in disk
    assert _promql("unknown-metric", "x.*") == _promql(None, "x.*")  # 回退默认


# ----------------------------------------------------------------------
# query_logs / query_metrics 请求构造（MockTransport）
# ----------------------------------------------------------------------
def _adapter(handler) -> RealDataSourceAdapter:
    transport = httpx.MockTransport(handler)
    return RealDataSourceAdapter(client=httpx.AsyncClient(transport=transport))


def _es_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/app-logs/_search"
    body = request.read().decode()
    assert '"app.service.keyword":"order-service"' in body
    return httpx.Response(200, json={
        "hits": {"hits": [
            {"_source": {"app": {"@timestamp": "t", "level": "ERROR",
                                 "service": "order-service", "message": "boom"}}}
        ]},
    })


def _prom_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/v1/query"
    assert "container_cpu_usage_seconds_total" in request.url.params["query"]
    return httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "vector", "result": [
            {"metric": {}, "value": [1787681406.15, "0.5"]},
        ]},
    })


async def test_query_logs_builds_es_request() -> None:
    ds = _adapter(_es_handler)
    out = await ds.query_logs(service="order-service", level="ERROR")
    assert out["found"] is True
    assert out["logs"][0]["message"] == "boom"
    assert out["logs"][0]["service"] == "order-service"
    await ds.aclose()


async def test_query_metrics_builds_prom_request() -> None:
    ds = _adapter(_prom_handler)
    out = await ds.query_metrics(service="order-service", metric="cpu")
    assert out["value"] == 0.5
    await ds.aclose()


# ----------------------------------------------------------------------
# 工具绑定（§5.2：签名不变，数据源可切换）
# ----------------------------------------------------------------------
async def test_build_l1_tools_with_datasource() -> None:
    from agentflow.agents.tools import build_l1_tools

    ds = RealDataSourceAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(_prom_handler)))
    tools = build_l1_tools("metrics-analyst", use_mock=True, datasource=ds)
    names = {t["name"] for t in tools}
    assert "query_metrics" in names
    qm = next(t for t in tools if t["name"] == "query_metrics")
    out = await qm["func"](service="order-service", metric="cpu")
    assert out["value"] == 0.5  # 真实 adapter 实现，非 mock
    await ds.aclose()
