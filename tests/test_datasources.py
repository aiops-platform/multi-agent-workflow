"""数据源适配器单测（mock httpx transport，不依赖真实 testbed）。

- ``_promql``：PromQL 表达式生成
- query_logs / query_metrics：请求构造 + 解析（MockTransport）
- build_l1_tools 数据源绑定：工具签名不变，实现指向真实 adapter
（真实 testbed 联调验证在会话记录中完成：ES/Prometheus/kubectl 三源均已实测）
"""
from __future__ import annotations

import httpx

from agentflow.agents.datasources import RealDataSourceAdapter, _promql


# ----------------------------------------------------------------------
# PromQL 表达式
# ----------------------------------------------------------------------
def test_promql_by_metric() -> None:
    """metric 名与 MetricsEvidenceSchema 字段对齐，且各指标表达式互不相同。"""
    cpu = _promql("cpu_percent", "order-service.*")
    assert "container_cpu_usage_seconds_total" in cpu
    assert 'pod=~"order-service.*"' in cpu
    assert "container_spec_cpu_quota" in cpu  # 除以 limit 得百分比

    disk = _promql("disk_percent", "warranty-service.*")
    assert "data_disk_free_bytes" in disk and "data_disk_total_bytes" in disk

    err = _promql("error_rate", "order-service.*")
    assert "http_server_requests_seconds_count" in err
    assert "5.." in err

    lat = _promql("p95_latency_ms", "order-service.*")
    assert "http_server_requests_seconds_sum" in lat

    # 五个指标必须产生**互不相同**的表达式（曾因命名错配全部回退成同一条 CPU 查询，
    # 致 metrics-analyst 拿到 5 个一模一样的数字并据此误判「CPU 空闲」）
    exprs = [
        _promql(m, "order-service.*")
        for m in ("cpu_percent", "memory_percent", "disk_percent", "error_rate", "p95_latency_ms")
    ]
    assert len(set(exprs)) == len(exprs)


def test_promql_unknown_metric_raises() -> None:
    """未知 metric 必须报错，不得静默兜底（旧行为回退 CPU → 语义错位的真实数字）。"""
    import pytest

    from agentflow.agents.datasources import DataSourceError

    with pytest.raises(DataSourceError) as ei:
        _promql("unknown-metric", "x.*")
    msg = str(ei.value)
    assert "未知 metric" in msg
    # 错误信息须给出可用清单，供 agent 自我纠正
    for m in ("cpu_percent", "memory_percent", "disk_percent", "error_rate", "p95_latency_ms"):
        assert m in msg


def test_promql_escape_hatch() -> None:
    """promql:/cadvisor: 前缀可直下钻任意表达式（绕过映射）。"""
    assert _promql('promql:up{job="x"}', "svc") == 'up{job="x"}'
    assert _promql("cadvisor:container_memory_usage_bytes", "svc") == "container_memory_usage_bytes"


def test_promql_guards_division_by_zero() -> None:
    """分母加 >0 过滤：容器未设 limit 时 limit=0 → 相除得 +Inf（实测过）。

    Inf/NaN 会被 agent 当成真实数字（如「内存爆了」），过滤后退化为「无数据」。
    """
    assert "> 0" in _promql("memory_percent", "svc.*")
    assert "> 0" in _promql("disk_percent", "svc.*")


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
    """Prometheus mock：回固定值 0.5（对任意合法 query 都返回，逃生舱测试也用）。"""
    assert request.url.path == "/api/v1/query"
    assert request.url.params["query"]  # query 非空即可（表达式正确性由 _promql 单测覆盖）
    return httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "vector", "result": [
            {"metric": {}, "value": [1787681406.15, "0.5"]},
        ]},
    })


def _trace_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/app-logs/_search"
    return httpx.Response(200, json={
        "hits": {"hits": [
            {"_source": {"app": {"@timestamp": "t1", "level": "INFO", "service": "order-service",
                                 "traceId": "tr-1", "message": "结账请求进入"}}},
            {"_source": {"app": {"@timestamp": "t2", "level": "ERROR", "service": "warranty-service",
                                 "traceId": "tr-1", "message": "查询三包期失败: fin 没有传"}}},
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
    out = await ds.query_metrics(service="order-service", metric="cpu_percent")
    assert out["value"] == 0.5
    await ds.aclose()


async def test_get_trace_reconstructs_chain_and_failing_span() -> None:
    """get_trace：按 traceId 重建调用链，判定故障 span（下游 warranty）。"""
    ds = _adapter(_trace_handler)
    out = await ds.get_trace(trace_id="tr-1")
    services = {s["service"] for s in out["chain"]}
    assert services == {"order-service", "warranty-service"}
    assert out["failing_service"] == "warranty-service"  # ERROR 未完成的服务
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
    out = await qm["func"](service="order-service", metric="cpu_percent")
    assert out["value"] == 0.5  # 真实 adapter 实现，非 mock
    await ds.aclose()


# ----------------------------------------------------------------------
# 逃生舱配额（防开放式探索耗尽 ReAct 轮次）
# ----------------------------------------------------------------------
async def test_escape_hatch_quota_per_node() -> None:
    """promql:/cadvisor: 每节点限 _ESCAPE_HATCH_LIMIT 次，超限报错且提示纠正。

    回归背景：逃生舱无上限时，metrics-analyst 用它逐字母枚举指标名跑了 54 次
    工具调用，轮次耗尽 → 整个节点无输出（promql: 是「下钻口」不是探索工具）。
    """
    import pytest

    from agentflow.agents.datasources import _ESCAPE_HATCH_LIMIT, DataSourceError
    from agentflow.exec_context import current_node

    ds = _adapter(_prom_handler)
    tok = current_node.set("metrics")
    try:
        for _ in range(_ESCAPE_HATCH_LIMIT):  # 额度内放行
            await ds.query_metrics(service="s", metric="promql:up")
        with pytest.raises(DataSourceError) as ei:
            await ds.query_metrics(service="s", metric="promql:up")
        assert "逃生舱" in str(ei.value)
        assert "cpu_percent" in str(ei.value)  # 给出纠正方向
    finally:
        current_node.reset(tok)
    await ds.aclose()


async def test_escape_hatch_quota_isolated_per_node() -> None:
    """配额按节点隔离（ContextVar）——一个节点耗尽不影响另一个。"""
    from agentflow.agents.datasources import _ESCAPE_HATCH_LIMIT
    from agentflow.exec_context import current_node

    ds = _adapter(_prom_handler)
    tok = current_node.set("node-a")
    try:
        for _ in range(_ESCAPE_HATCH_LIMIT):
            await ds.query_metrics(service="s", metric="promql:up")
    finally:
        current_node.reset(tok)
    # 换节点：额度应重新计（否则并行波次会互相误伤）
    tok = current_node.set("node-b")
    try:
        out = await ds.query_metrics(service="s", metric="promql:up")
        assert out["count"] == 1
    finally:
        current_node.reset(tok)
    await ds.aclose()
