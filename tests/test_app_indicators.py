"""Smart Inspection 应用指标：纯逻辑 + 组装 + 缓存/降级。

无网络：注入 ``FakePrometheusClient`` 替换 ``PrometheusClient``。
样本形状照抄实测结果（3 个 pod，`max by (pod)` 后只剩 pod 标签）。
"""
import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from agentflow.config import Settings
from agentflow.datasource.app_indicators import (
    K_CPU_LIMIT,
    K_CPU_USED,
    K_FS_READ,
    K_FS_WRITE,
    K_LIVE_PODS,
    K_MEM_LIMIT,
    K_MEM_USED,
    K_NET_RX,
    K_NET_TX,
    K_START_TIME,
    AppIndicatorsService,
    build_pod_regex,
    build_queries,
    build_rows,
    classify_status,
    envelope,
    failure,
    format_bytes,
    format_rate,
    format_uptime,
    local_time_str,
    percent,
    service_id,
    service_of_pod,
)
from agentflow.datasource.prometheus import DataSourceError, QueryOutcome, Series

PODS = {
    "order-service": "order-service-79dd596b47-lnt5p",
    "warranty-service": "warranty-service-7f5bc6dbcd-5l8sb",
    "gateway-service": "gateway-service-8657567584-bplzb",
}
NOW = 1_789_530_000.0


def _settings(**over) -> Settings:
    base = {
        "prometheus_namespace": "order",
        "prometheus_staleness_sec": 60.0,
        "app_indicators_cache_ttl": 0.0,
        "app_indicators_warn_cpu": 70.0,
        "app_indicators_crit_cpu": 90.0,
        "app_indicators_warn_mem": 70.0,
        "app_indicators_crit_mem": 90.0,
    }
    base.update(over)
    return Settings(**base)


# ────────────────────────── 纯函数 ──────────────────────────


def test_build_pod_regex_is_re2_safe() -> None:
    """回归：``re.escape`` 会把 ``-`` 转成 ``\\-``，RE2 不认 → Prometheus HTTP 400。

    这个 bug 实测让全部 10 条查询同时挂掉（每轮都是 OFFLINE），且错误只显示
    "HTTP 400"，排查成本很高。
    """
    rx = build_pod_regex(["order-service", "warranty-service"])
    assert "\\-" not in rx
    assert rx == "(warranty-service|order-service)-.*"


def test_build_pod_regex_sorted_by_length_desc() -> None:
    """「order」必须排在「order-service」后面，否则前缀会先被短的吃掉。"""
    rx = build_pod_regex(["order", "order-service"])
    assert rx == "(order-service|order)-.*"


def test_build_pod_regex_escapes_only_real_meta() -> None:
    rx = build_pod_regex(["svc.v2", "a+b"])
    assert "svc\\.v2" in rx and "a\\+b" in rx


def test_build_pod_regex_empty() -> None:
    assert build_pod_regex([]) == ""


def test_service_of_pod_uses_longest_prefix_with_boundary() -> None:
    """``order-service`` 不得吃掉 ``order-service-v2-xxx`` 这个**另一个**服务。"""
    names = ["order-service", "order-service-v2", "order"]
    assert service_of_pod("order-service-v2-abc-xyz", names) == "order-service-v2"
    assert service_of_pod("order-service-79dd-lnt5p", names) == "order-service"
    assert service_of_pod("order-abc-def", names) == "order"


def test_service_of_pod_requires_dash_boundary() -> None:
    # 前缀必须落在 "-" 边界上：order 不该匹配 orderxx-1-2
    assert service_of_pod("orderxx-1-2", ["order"]) is None
    assert service_of_pod("unknown-1-2", ["order-service"]) is None


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, None), (-1, None), (float("inf"), None), (45, "45s"), (600, "10m"), (3600 * 5 + 60 * 30, "5h 30m"), (86400 * 3 + 3600 * 12, "3d 12h")],
)
def test_format_uptime(seconds, expected) -> None:
    assert format_uptime(seconds) == expected


def test_format_bytes_and_rate() -> None:
    assert format_bytes(248) == "248 B"
    assert format_bytes(3300) == "3.2 kB"
    assert format_rate(3300) == "3.2 kB/s"
    assert format_bytes(None) is None
    assert format_bytes(float("nan")) is None


def test_local_time_str_renders_local_wall_clock() -> None:
    """时间字段是给用户直接看的，必须落在本地时区（仓库内部统一 UTC，
    但直接用 UTC 展示会差 8 小时）。"""
    ts = 1_789_530_000.0
    s = local_time_str(ts)
    assert len(s) == 19 and s[4] == "-" and s[13] == ":"
    # 必须是**本地**墙钟，而不是 UTC
    assert s == datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_percent_guards_non_finite_and_zero_denominator() -> None:
    """分母为 0（容器未设 limit）必须给 None，不能给 Inf——Inf 会破坏 JSON 序列化。"""
    assert percent(0.5, 1.0) == 50.0
    assert percent(1.0, 0) is None            # 未设 limit
    assert percent(1.0, None) is None
    assert percent(None, 1.0) is None
    assert percent(float("inf"), 1.0) is None
    assert percent(1.0, float("inf")) is None
    assert percent(float("nan"), 1.0) is None


def test_service_id_stable_and_numeric() -> None:
    a, b = service_id("order-service"), service_id("order-service")
    assert a == b and isinstance(a, int) and 100_000 <= a < 1_000_000
    assert service_id("warranty-service") != a


@pytest.mark.parametrize(
    "cpu,mem,up_max,live,expected",
    [
        (10.0, 20.0, 1.0, 1, "HEALTHY"),
        (75.0, 20.0, 1.0, 1, "WARNING"),
        (10.0, 95.0, 1.0, 1, "CRITICAL"),
        (None, None, 1.0, 1, "UNKNOWN"),      # 活着但无可算指标
        (10.0, 20.0, 0.0, 1, "OFFLINE"),      # 所有实例抓取失败
        (10.0, 20.0, 1.0, 0, "OFFLINE"),      # 有 target 无指标
    ],
)
def test_classify_status(cpu, mem, up_max, live, expected) -> None:
    assert classify_status(
        cpu, mem, up_max=up_max, live_pods=live,
        warn_cpu=70.0, crit_cpu=90.0, warn_mem=70.0, crit_mem=90.0,
    ) == expected


def test_build_queries_uses_container_nonempty_not_POD() -> None:
    """回归：``container!="POD"`` 在本集群会算错（sandbox 的 container 标签是缺失的，
    该写法会把 pod 级 + sandbox + 应用容器三条序列全留下，CPU 接近翻倍）。
    """
    qs = build_queries("(a|b)-.*", namespace="order", staleness_sec=60, use_staleness=True)
    for key in (K_CPU_USED, K_MEM_USED, K_FS_READ, K_FS_WRITE):
        assert 'container!=""' in qs[key]
        assert 'container!="POD"' not in qs[key]
    # 网络指标没有 container 标签，绝不能加 container 过滤
    for key in (K_NET_RX, K_NET_TX):
        assert 'container=' not in qs[key] and 'container!=' not in qs[key]
        assert 'interface="eth0"' in qs[key]
    assert "time() - 60" in qs[K_LIVE_PODS]


def test_build_queries_omits_namespace_when_blank() -> None:
    qs = build_queries("(a)-.*", namespace="", staleness_sec=60, use_staleness=False)
    assert "namespace" not in qs[K_CPU_USED]
    assert "time() -" not in qs[K_LIVE_PODS]


# ────────────────────────── 组装 ──────────────────────────


def _series(pod: str, value: float, ts: float = NOW) -> Series:
    return Series(metric={"pod": pod}, value=value, ts=ts)


def _outcomes(**over) -> dict[str, QueryOutcome]:
    """默认：3 个 pod，CPU 用 0.25 核 / 限额 1 核；内存 256MiB / limit 512MiB。"""
    base = {
        K_LIVE_PODS: [_series(p, NOW) for p in PODS.values()],
        K_CPU_USED: [_series(p, 0.25) for p in PODS.values()],
        K_CPU_LIMIT: [_series(p, 1.0) for p in PODS.values()],
        K_MEM_USED: [_series(p, 256 * 1024 * 1024) for p in PODS.values()],
        K_MEM_LIMIT: [_series(p, 512 * 1024 * 1024) for p in PODS.values()],
        K_NET_RX: [_series(p, 1024.0) for p in PODS.values()],
        K_NET_TX: [_series(p, 2048.0) for p in PODS.values()],
        K_FS_READ: [_series(p, 0.0) for p in PODS.values()],
        K_FS_WRITE: [_series(p, 4096.0) for p in PODS.values()],
        K_START_TIME: [_series(p, NOW - 3600 * 5) for p in PODS.values()],
    }
    base.update(over)
    return {k: QueryOutcome(key=k, series=v) for k, v in base.items()}


def _services(**up) -> dict:
    return {name: {"up_max": up.get(name, 1.0), "up_min": up.get(name, 1.0)} for name in PODS}


def test_build_rows_happy_path() -> None:
    rows = build_rows(_services(), _outcomes(), set(PODS.values()), _settings())
    assert len(rows) == 3
    by = {r["serviceName"]: r for r in rows}
    order = by["order-service"]
    assert order["cpuUsage"] == 25.0
    assert order["memoryUsage"] == 50.0
    assert order["status"] == "HEALTHY"
    assert order["uptime"] == "5h 0m"
    assert order["netIO"] == "1.0 kB / 2.0 kB"     # 分隔符必须是 " / "
    assert order["netIn"] == "1.0 kB"
    assert order["blockIO"] == "4.0 kB/s"
    assert order["enabled"] is True
    assert order["lastUpdateTime"] is not None


def test_build_rows_null_fields_are_honest_not_fabricated() -> None:
    """六个无数据源字段必须 null，**尤其 alertCount**——0 的语义是「无告警」，
    而真相是「告警规则压根没加载」。"""
    rows = build_rows(_services(), _outcomes(), set(PODS.values()), _settings())
    r = rows[0]
    assert r["agentName"] is None
    assert r["serviceType"] is None
    assert r["owner"] is None
    assert r["description"] is None
    assert r["restartCount"] is None
    assert r["alertCount"] is None


def test_build_rows_excludes_dead_pods() -> None:
    """已销毁 pod 的陈旧样本不得计入（Prometheus staleness 会滞留约 5 分钟）。

    这里让 order 的旧 pod 仍有样本，但不在 live 集合里。
    """
    stale = "order-service-OLDOLDOLD-aaaaa"
    outcomes = _outcomes(**{
        K_CPU_USED: [_series(p, 0.25) for p in PODS.values()] + [_series(stale, 99.0)],
        K_CPU_LIMIT: [_series(p, 1.0) for p in PODS.values()] + [_series(stale, 1.0)],
    })
    rows = build_rows(_services(), outcomes, set(PODS.values()), _settings())
    order = next(r for r in rows if r["serviceName"] == "order-service")
    # 99 核没被算进来，仍是 0.25/1.0 = 25%
    assert order["cpuUsage"] == 25.0
    assert order["pods"] == [PODS["order-service"]]


def test_build_rows_zero_denominator_gives_none_not_inf() -> None:
    """未设 limit 时百分比无定义 → None（诚实），且不得产出 Inf/NaN。"""
    outcomes = _outcomes(**{K_CPU_LIMIT: [_series(p, 0.0) for p in PODS.values()]})
    rows = build_rows(_services(), outcomes, set(PODS.values()), _settings())
    assert all(r["cpuUsage"] is None for r in rows)
    # 内存仍可算，故不是 UNKNOWN
    assert all(r["status"] == "HEALTHY" for r in rows)


def test_build_rows_no_metrics_at_all_is_unknown() -> None:
    outcomes = _outcomes(**{K_CPU_USED: [], K_CPU_LIMIT: [], K_MEM_USED: [], K_MEM_LIMIT: []})
    rows = build_rows(_services(), outcomes, set(PODS.values()), _settings())
    assert all(r["status"] == "UNKNOWN" for r in rows)


def test_build_rows_offline_when_no_live_pods() -> None:
    rows = build_rows(_services(), _outcomes(), set(), _settings())
    assert all(r["status"] == "OFFLINE" for r in rows)
    assert all(r["cpuUsage"] is None and r["uptime"] is None for r in rows)


def test_envelope_serializes_with_allow_nan_false() -> None:
    """回归：FastAPI 默认 allow_nan=True 会把 Inf 写成 ``Infinity``，
    浏览器 ``res.json()`` 直接抛错 → 整页降级到 mock。"""
    rows = build_rows(_services(), _outcomes(), set(PODS.values()), _settings())
    env = envelope(rows, {"source": "prometheus"})
    assert set(env) == {"success", "data", "error"}
    assert set(env["data"]) >= {"content", "page", "size", "total", "totalPages", "hasNext"}
    assert env["data"]["total"] == 3
    json.dumps(env, allow_nan=False)   # 不抛即通过


def test_failure_envelope_has_null_data() -> None:
    env = failure("Prometheus 不可达")
    assert env == {"success": False, "data": None, "error": "Prometheus 不可达"}


# ────────────────────────── Service（缓存 / 降级 / 失败）──────────────────────────


class FakePrometheusClient:
    """按 expr 里的识别串返回预置 series；可注入异常。"""

    def __init__(self, up=None, by_key=None, fail_all: str | None = None) -> None:
        self.up = up if up is not None else [Series(metric={"service": s, "job": "app-metrics"}, value=1.0, ts=NOW) for s in PODS]
        self.by_key = by_key or {}
        self.fail_all = fail_all
        self.calls: list[str] = []

    _MATCHERS: ClassVar[list[tuple[str, str]]] = [
        ("container_last_seen", K_LIVE_PODS),
        ("container_spec_cpu_quota", K_CPU_LIMIT),
        ("container_cpu_usage_seconds_total", K_CPU_USED),
        ("container_spec_memory_limit_bytes", K_MEM_LIMIT),
        ("container_memory_working_set_bytes", K_MEM_USED),
        ("container_network_receive_bytes_total", K_NET_RX),
        ("container_network_transmit_bytes_total", K_NET_TX),
        ("container_fs_reads_bytes_total", K_FS_READ),
        ("container_fs_writes_bytes_total", K_FS_WRITE),
        ("container_start_time_seconds", K_START_TIME),
    ]

    def _classify(self, expr: str) -> str | None:
        for needle, key in self._MATCHERS:
            if needle in expr:
                return key
        return None

    async def query(self, expr: str) -> list[Series]:
        self.calls.append(expr)
        if self.fail_all:
            raise DataSourceError(self.fail_all)
        if expr.startswith("up{"):
            return self.up
        key = self._classify(expr)
        return self.by_key.get(key, [])

    async def query_many(self, exprs) -> dict[str, QueryOutcome]:
        out: dict[str, QueryOutcome] = {}
        for key, expr in exprs.items():
            try:
                out[key] = QueryOutcome(key=key, series=await self.query(expr))
            except DataSourceError as exc:
                out[key] = QueryOutcome(key=key, error=str(exc))
        return out

    async def aclose(self) -> None:
        pass


async def test_snapshot_happy_path() -> None:
    fake = FakePrometheusClient(by_key={
        K_LIVE_PODS: [_series(p, NOW) for p in PODS.values()],
        K_CPU_USED: [_series(p, 0.25) for p in PODS.values()],
        K_CPU_LIMIT: [_series(p, 1.0) for p in PODS.values()],
        K_MEM_USED: [_series(p, 256 * 1048576) for p in PODS.values()],
        K_MEM_LIMIT: [_series(p, 512 * 1048576) for p in PODS.values()],
        K_START_TIME: [_series(p, NOW - 7200) for p in PODS.values()],
    })
    svc = AppIndicatorsService(fake, _settings())
    env = await svc.snapshot()
    assert env["success"] is True
    assert env["data"]["total"] == 3
    assert env["data"]["meta"]["podsLive"] == 3
    assert env["data"]["meta"]["warnings"] == []
    assert all(r["status"] == "HEALTHY" for r in env["data"]["content"])
    json.dumps(env, allow_nan=False)


async def test_snapshot_returns_failure_envelope_on_upstream_error() -> None:
    fake = FakePrometheusClient(fail_all="Prometheus 不可达（ConnectError）")
    svc = AppIndicatorsService(fake, _settings())
    env = await svc.snapshot()
    assert env["success"] is False
    assert env["data"] is None
    assert "不可达" in env["error"]


async def test_snapshot_empty_discovery_returns_empty_content() -> None:
    """没有服务时给 ``[]``（真值），前端不会掉进 mock 分支。"""
    svc = AppIndicatorsService(FakePrometheusClient(up=[]), _settings())
    env = await svc.snapshot()
    assert env["success"] is True and env["data"]["content"] == []


async def test_snapshot_warns_when_staleness_query_fails() -> None:
    """降级重查必须留下 warning，不能静默。"""
    fake = FakePrometheusClient(by_key={
        K_LIVE_PODS: [_series(p, NOW) for p in PODS.values()],
        K_CPU_USED: [_series(p, 0.25) for p in PODS.values()],
        K_CPU_LIMIT: [_series(p, 1.0) for p in PODS.values()],
    })
    original = fake.query

    async def flaky(expr: str):
        if "time() -" in expr:
            raise DataSourceError("bad_data: invalid parameter")
        return await original(expr)

    fake.query = flaky  # type: ignore[method-assign]
    svc = AppIndicatorsService(fake, _settings())
    env = await svc.snapshot()
    assert env["success"] is True
    assert any("陈旧样本" in w for w in env["data"]["meta"]["warnings"])


async def test_snapshot_retries_without_namespace_when_empty() -> None:
    """namespace 配错时去掉过滤重查，并留下 warning。"""
    fake = FakePrometheusClient(by_key={})
    original = fake.query

    async def ns_aware(expr: str):
        if 'namespace="order"' in expr:
            return []
        return await original(expr)

    fake.query = ns_aware  # type: ignore[method-assign]
    svc = AppIndicatorsService(fake, _settings())
    env = await svc.snapshot()
    assert env["success"] is True


async def test_cache_ttl_behaviour() -> None:
    fake = FakePrometheusClient(by_key={
        K_LIVE_PODS: [_series(p, NOW) for p in PODS.values()],
        K_CPU_USED: [_series(p, 0.25) for p in PODS.values()],
        K_CPU_LIMIT: [_series(p, 1.0) for p in PODS.values()],
    })
    svc = AppIndicatorsService(fake, _settings(app_indicators_cache_ttl=60.0))
    await svc.snapshot()
    n1 = len(fake.calls)
    await svc.snapshot()
    assert len(fake.calls) == n1          # 命中缓存

    svc2 = AppIndicatorsService(fake, _settings(app_indicators_cache_ttl=0.0))
    await svc2.snapshot()
    n2 = len(fake.calls)
    await svc2.snapshot()
    assert len(fake.calls) > n2           # 关缓存则每次都查


async def test_failure_is_not_cached() -> None:
    """失败不落缓存：Prometheus 恢复后下一次立即拿到真实数据。"""
    fake = FakePrometheusClient(fail_all="boom")
    svc = AppIndicatorsService(fake, _settings(app_indicators_cache_ttl=60.0))
    assert (await svc.snapshot())["success"] is False

    fake.fail_all = None
    fake.by_key = {
        K_LIVE_PODS: [_series(p, NOW) for p in PODS.values()],
        K_CPU_USED: [_series(p, 0.25) for p in PODS.values()],
        K_CPU_LIMIT: [_series(p, 1.0) for p in PODS.values()],
    }
    assert (await svc.snapshot())["success"] is True
