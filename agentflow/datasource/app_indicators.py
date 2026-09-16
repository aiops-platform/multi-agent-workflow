"""Smart Inspection 应用指标：Prometheus 发现 → 查询 → DTO → 遗留前端信封。

设计要点（与 ``prometheus.py`` 的分工）：

- **PromQL 只做过滤 + 求和，除法/阈值/格式化全在 Python**。原因：容器未设 limit 时
  分母为 0，PromQL 会给出 ``Inf``；FastAPI 默认 ``allow_nan=True`` 会把它序列化成
  ``Infinity``，浏览器 ``res.json()`` 直接抛错 → 整页降级。在 Python 里守卫得住，且
  100% 可单测。
- 纯函数（正则/映射/格式化/阈值）与 I/O 分离，便于单测。

⚠️ **诚实优先**：取不到真值的字段一律 ``null``，不编造（``agentName`` / ``serviceType``
/ ``owner`` / ``description`` / ``restartCount`` / ``alertCount``）。``alertCount`` 尤其
不能给 0——0 的语义是「无告警」，而真相是「告警规则压根没加载」。

⚠️ 架构例外见 ``datasource/__init__.py``。
"""
from __future__ import annotations

import logging
import math
import time
import zlib
from collections.abc import Iterable
from datetime import UTC, datetime

from ..config import Settings
from .prometheus import PrometheusClient, QueryOutcome
from .service_meta import K8sServiceMetaSource

logger = logging.getLogger("agentflow.datasource")

# rate()/irate() 的窗口。scrape_interval=5s → 24 个点，够稳且不过度平滑。
RATE_WINDOW = "2m"

# 查询 key（也是 ``query_many`` 的字典键）
K_LIVE_PODS = "live_pods"
K_CPU_USED = "cpu_used"
K_CPU_LIMIT = "cpu_limit"
K_MEM_USED = "mem_used"
K_MEM_LIMIT = "mem_limit"
K_NET_RX = "net_rx"
K_NET_TX = "net_tx"
K_FS_READ = "fs_read"
K_FS_WRITE = "fs_write"
K_START_TIME = "start_time"

STATUS_HEALTHY = "HEALTHY"
STATUS_WARNING = "WARNING"
STATUS_CRITICAL = "CRITICAL"
STATUS_OFFLINE = "OFFLINE"
STATUS_UNKNOWN = "UNKNOWN"


# ────────────────────────── 纯函数 ──────────────────────────


# PromQL 用的是 RE2（Go regexp），**不是 Python re**。两者对转义序列的宽容度不同：
# `re.escape("order-service")` 会给出 `order\-service`，Python re 接受，而 RE2 直接报
# 「invalid escape sequence」→ Prometheus 返回 HTTP 400（整批查询全挂）。
# 故自建转义：只转义真正的元字符，`-` 在字符类之外是字面量，必须原样保留。
_RE2_META = frozenset("\\^$.*+?()[]{}|")


def _escape_re2(name: str) -> str:
    return "".join(f"\\{c}" if c in _RE2_META else c for c in name)


def build_pod_regex(services: Iterable[str]) -> str:
    """服务名 → pod 正则。**按长度降序**，避免 ``order`` 抢在 ``order-service`` 前匹配。"""
    ordered = sorted({s for s in services if s}, key=len, reverse=True)
    if not ordered:
        return ""
    return "(" + "|".join(_escape_re2(s) for s in ordered) + ")-.*"


def service_of_pod(pod: str, services: Iterable[str]) -> str | None:
    """pod 名 → 服务名，**最长前缀 + ``-`` 边界**。

    边界必不可少：否则 ``order-service`` 会吃掉 ``order-service-v2-xxx`` 这个别的服务。
    """
    best: str | None = None
    for s in services:
        if not s or not pod.startswith(s + "-"):
            continue
        if best is None or len(s) > len(best):
            best = s
    return best


def format_bytes(value: float | None) -> str | None:
    """字节数 → 人类可读（无速率后缀），与遗留前端 mock 的 ``netIn`` 同形。"""
    if value is None or not math.isfinite(value):
        return None
    v = abs(value)
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if v >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{value:.0f} B"


def format_rate(value: float | None) -> str | None:
    """字节/秒 → 人类可读（带 ``/s``），与 mock 的 ``disk: '8.2 MB/s'`` 同形。"""
    base = format_bytes(value)
    return None if base is None else f"{base}/s"


def format_uptime(seconds: float | None) -> str | None:
    """秒 → ``3d 12h`` / ``5h 30m`` / ``12m`` / ``45s``（与遗留前端保持一致的粗粒度）。"""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def local_time_str(ts: float) -> str:
    """时间戳 → 宿主本地时区 ``YYYY-MM-DD HH:MM:SS``。

    本模块的时间字段（``lastUpdateTime`` / ``generatedAt``）是**直接展示给用户**的，
    故转成本地时区——仓库内部时间统一用 UTC，但那样前端会看到差 8 小时的时间。
    显式给 `tz` 再 `.astimezone()`，避免 naive datetime。
    """
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def percent(numerator: float | None, denominator: float | None) -> float | None:
    """百分比，**带非有限守卫**。

    分母 ≤ 0（未设 limit）或任一值非有限 → ``None``（"无数据"，诚实）。
    直接相除会得到 ``Inf``/``NaN``，序列化出去会让浏览器 ``res.json()`` 抛错。
    """
    if numerator is None or denominator is None:
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator):
        return None
    if denominator <= 0:
        return None
    value = 100.0 * numerator / denominator
    if not math.isfinite(value):
        return None
    return round(value, 2)


def service_id(name: str) -> int:
    """服务名 → 稳定数字 id。

    **必须是数字**：前端 ``openSIDetail(Number(id))`` 与 ``find(item.id === serviceId)``
    做严格比较，字符串会变成 NaN。用 crc32 保证跨轮询、跨重启不变。
    """
    return abs(zlib.crc32(name.encode("utf-8"))) % 900_000 + 100_000


def classify_status(
    cpu: float | None,
    mem: float | None,
    *,
    up_max: float | None,
    live_pods: int,
    warn_cpu: float,
    crit_cpu: float,
    warn_mem: float,
    crit_mem: float,
) -> str:
    """状态判定。值**永不为空**——前端 ``deriveStatus`` 会 ``toLowerCase()``，
    空值会走进一个引用了未定义变量的 fallback 分支（ReferenceError）。"""
    if up_max == 0 or live_pods == 0:
        return STATUS_OFFLINE
    if cpu is None and mem is None:
        # 活着且被抓到，但没有可算的指标（如未设 limit）。不编造，单独成状态。
        return STATUS_UNKNOWN
    if (cpu is not None and cpu >= crit_cpu) or (mem is not None and mem >= crit_mem):
        return STATUS_CRITICAL
    if (cpu is not None and cpu >= warn_cpu) or (mem is not None and mem >= warn_mem):
        return STATUS_WARNING
    return STATUS_HEALTHY


def build_queries(pod_regex: str, *, namespace: str, staleness_sec: float, use_staleness: bool) -> dict[str, str]:
    """构造本轮全部 PromQL。占位符统一在这里拼，便于单测与降级重查。

    三条从实测得来的硬约束：

    1. 过滤应用容器必须用 ``container!=""``。**不能用 ``container!="POD"``**——本集群
       sandbox 序列的 ``container`` 标签是**缺失**的（不是 ``"POD"``），``!="POD"`` 会把
       「pod 级 cgroup + sandbox + 应用容器」三条序列全留下，CPU 接近翻倍。
       ``container_fs_*`` 同样受影响（pod 级与容器级序列并存）。
    2. **网络指标没有 ``container`` 标签**（挂在 sandbox 上），只能按 pod + interface 过滤。
    3. CPU/内存拆成「用量」与「限额」两条分别求和，服务级百分比 = Σ用量/Σ限额，
       多副本时才数学正确。
    """
    ns = f',namespace="{namespace}"' if namespace else ""
    sel = f'pod=~"{pod_regex}",container!=""{ns}'
    net_sel = f'pod=~"{pod_regex}",interface="eth0"{ns}'
    staleness = f' > time() - {staleness_sec:g}' if use_staleness else ""

    return {
        # 活 pod 集合：新鲜度交给 Prometheus 的 time()，免疫宿主与 Prometheus 的时钟偏差
        K_LIVE_PODS: f'max by (pod) (container_last_seen{{{sel}}}{staleness})',
        K_CPU_USED: f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{sel}}}[{RATE_WINDOW}]))',
        K_CPU_LIMIT: f'sum by (pod) (container_spec_cpu_quota{{{sel}}}) / 100000',
        K_MEM_USED: f'sum by (pod) (container_memory_working_set_bytes{{{sel}}})',
        K_MEM_LIMIT: f'sum by (pod) (container_spec_memory_limit_bytes{{{sel}}})',
        K_NET_RX: f'sum by (pod) (rate(container_network_receive_bytes_total{{{net_sel}}}[{RATE_WINDOW}]))',
        K_NET_TX: f'sum by (pod) (rate(container_network_transmit_bytes_total{{{net_sel}}}[{RATE_WINDOW}]))',
        K_FS_READ: f'sum by (pod) (rate(container_fs_reads_bytes_total{{{sel}}}[{RATE_WINDOW}]))',
        K_FS_WRITE: f'sum by (pod) (rate(container_fs_writes_bytes_total{{{sel}}}[{RATE_WINDOW}]))',
        K_START_TIME: f'max by (pod) (container_start_time_seconds{{{sel}}})',
    }


# ────────────────────────── 组装 ──────────────────────────


def _by_pod(outcome: QueryOutcome | None) -> dict[str, tuple[float, float]]:
    """``QueryOutcome`` → ``{pod: (value, ts)}``。失败返回空字典。"""
    if outcome is None or outcome.error:
        return {}
    return {
        s.metric["pod"]: (s.value, s.ts)
        for s in outcome.series
        if s.metric.get("pod")
    }


def _sum_for(outcome: QueryOutcome | None, pods: list[str]) -> float | None:
    """对给定 pod 集合求和。一个都取不到 → ``None``（而非 0，0 会被当成真实值）。"""
    data = _by_pod(outcome)
    values = [data[p][0] for p in pods if p in data]
    if not values:
        return None
    total = sum(values)
    return total if math.isfinite(total) else None


def _max_ts(outcomes: Iterable[QueryOutcome | None], pods: list[str]) -> float | None:
    newest: float | None = None
    for outcome in outcomes:
        data = _by_pod(outcome)
        for p in pods:
            if p in data:
                ts = data[p][1]
                if newest is None or ts > newest:
                    newest = ts
    return newest


def build_rows(
    services: dict[str, dict[str, float | None]],
    outcomes: dict[str, QueryOutcome],
    live_pods: set[str],
    settings: Settings,
    service_meta: dict[str, dict] | None = None,
) -> list[dict]:
    """服务清单 + 查询结果 → DTO 列表。"""
    names = list(services)
    all_outcomes = list(outcomes.values())

    # 本轮样本的最新时间戳（Prometheus 时钟）——uptime 用它做基准，不用宿主 time.time()
    now_prom: float | None = None
    for outcome in all_outcomes:
        for s in outcome.series:
            if now_prom is None or s.ts > now_prom:
                now_prom = s.ts
    if now_prom is None:
        now_prom = time.time()

    rows: list[dict] = []
    for name in sorted(names):
        info = services[name]
        up_max = info.get("up_max")
        pods = sorted(p for p in live_pods if service_of_pod(p, names) == name)

        cpu_pct = mem_pct = None
        net_in = net_out = blk_read = blk_write = None
        uptime = None
        last_ts = _max_ts(all_outcomes, pods) if pods else None

        if pods:
            cpu_pct = percent(_sum_for(outcomes.get(K_CPU_USED), pods),
                              _sum_for(outcomes.get(K_CPU_LIMIT), pods))
            mem_pct = percent(_sum_for(outcomes.get(K_MEM_USED), pods),
                              _sum_for(outcomes.get(K_MEM_LIMIT), pods))
            net_in = _sum_for(outcomes.get(K_NET_RX), pods)
            net_out = _sum_for(outcomes.get(K_NET_TX), pods)
            blk_read = _sum_for(outcomes.get(K_FS_READ), pods)
            blk_write = _sum_for(outcomes.get(K_FS_WRITE), pods)

            starts = _by_pod(outcomes.get(K_START_TIME))
            start_ts = min((starts[p][0] for p in pods if p in starts), default=None)
            if start_ts is not None:
                uptime = format_uptime(now_prom - start_ts)

        read_rate = blk_read if blk_read is not None else None
        write_rate = blk_write if blk_write is not None else None
        if read_rate is None and write_rate is None:
            block_io = None
        else:
            total = (read_rate or 0.0) + (write_rate or 0.0)
            block_io = format_rate(total)

        # 展示元数据来自 Deployment label（声明配置，不是实测值）。
        # 没有对应 label 的服务照样返回 null —— 不编造，也不因为"配置里没有"就把它从列表里藏掉。
        meta = (service_meta or {}).get(name) or {}

        rows.append({
            "serviceId": service_id(name),
            "serviceName": name,
            "agentName": meta.get("agentName"),
            "serviceType": meta.get("type"),
            "owner": meta.get("owner"),
            # 以下三项仍无数据源，诚实返回 null（前端渲染为 "-"）。
            # TODO(v5.7): 接 MCP CMDB 后补 description；
            #             kube-state-metrics 部署后可补 restartCount；告警规则挂载后可补 alertCount。
            "description": None,
            "restartCount": None,
            "alertCount": None,
            "cpuUsage": cpu_pct,
            "memoryUsage": mem_pct,
            "blockIO": block_io,
            "netIO": (f"{format_bytes(net_in)} / {format_bytes(net_out)}"
                      if net_in is not None or net_out is not None else None),
            "netIn": format_bytes(net_in),
            "netOut": format_bytes(net_out),
            "uptime": uptime,
            "status": classify_status(
                cpu_pct, mem_pct,
                up_max=up_max, live_pods=len(pods),
                warn_cpu=settings.app_indicators_warn_cpu,
                crit_cpu=settings.app_indicators_crit_cpu,
                warn_mem=settings.app_indicators_warn_mem,
                crit_mem=settings.app_indicators_crit_mem,
            ),
            # "在采集范围内"——能被发现就说明 pod 带 prometheus.io/scrape 注解
            "enabled": True,
            "lastUpdateTime": local_time_str(last_ts) if last_ts is not None else None,
            # 前端忽略，仅供 curl / 排查
            "pods": pods,
        })
    return rows


def envelope(content: list[dict], meta: dict) -> dict:
    """遗留前端期望的信封形状（**不是** agentflow 的通用约定，只此一处，勿泛化）。"""
    return {
        "success": True,
        "data": {
            "content": content,
            "page": 0,
            "size": len(content),
            "total": len(content),
            "totalPages": 1,
            "hasNext": False,
            "meta": meta,
        },
        "error": None,
    }


def failure(message: str) -> dict:
    """失败信封。**不暴露内部地址**（沿用仓库「不回显 DSN」惯例）。"""
    return {"success": False, "data": None, "error": message}


# ────────────────────────── Service ──────────────────────────


class AppIndicatorsService:
    """单次快照：发现 → 查询 → 组装。带短缓存与两处降级重查。"""

    def __init__(
        self,
        client: PrometheusClient,
        settings: Settings,
        meta_source: K8sServiceMetaSource | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._meta_source = meta_source
        self._cache: tuple[float, dict] | None = None

    async def snapshot(self) -> dict:
        ttl = self._settings.app_indicators_cache_ttl
        if ttl > 0 and self._cache is not None:
            cached_at, cached = self._cache
            if time.monotonic() - cached_at < ttl:
                return cached

        result = await self._snapshot_uncached()
        # 只缓存成功结果：Prometheus 恢复后下一次立即拿到新数据，不被失败结果钉住
        if ttl > 0 and result.get("success"):
            self._cache = (time.monotonic(), result)
        return result

    async def _snapshot_uncached(self) -> dict:
        s = self._settings
        warnings: list[str] = []

        try:
            up = await self._client.query(f'up{{job="{s.prometheus_job}"}}')
        except Exception as exc:  # noqa: BLE001 — 这是 API 的兜底边界：
            # 任何异常（含意外类型）都不该变成 500，否则前端 `!res.ok` 会丢弃 body、
            # 页面拿不到错误文案，只能显示"未知错误"。
            logger.warning("app-indicators 发现失败：%s", exc)
            return failure(f"应用指标后端不可用：{exc}")

        services: dict[str, dict[str, float | None]] = {}
        for series in up:
            name = series.metric.get("service")
            if not name:
                warnings.append("发现到无 service 标签的 target，已跳过")
                continue
            info = services.setdefault(name, {"up_max": None, "up_min": None})
            info["up_max"] = series.value if info["up_max"] is None else max(info["up_max"], series.value)
            info["up_min"] = series.value if info["up_min"] is None else min(info["up_min"], series.value)

        if not services:
            return envelope([], self._meta(0, 0, warnings))

        # 展示元数据（owner/type/agentName）：失败只记 warning，指标照常返回
        service_meta: dict[str, dict] = {}
        if self._meta_source is not None:
            service_meta, meta_warning = await self._meta_source.fetch()
            if meta_warning:
                warnings.append(meta_warning)

        pod_regex = build_pod_regex(services)
        outcomes, live_pods = await self._collect(pod_regex, warnings)

        rows = build_rows(services, outcomes, live_pods, s, service_meta)
        return envelope(rows, self._meta(len(services), len(live_pods), warnings))

    async def _collect(
        self, pod_regex: str, warnings: list[str]
    ) -> tuple[dict[str, QueryOutcome], set[str]]:
        """跑一轮查询，必要时降级重查。返回 (outcomes, live_pods)。"""
        s = self._settings

        async def run(namespace: str, use_staleness: bool) -> dict[str, QueryOutcome]:
            return await self._client.query_many(build_queries(
                pod_regex, namespace=namespace,
                staleness_sec=s.prometheus_staleness_sec, use_staleness=use_staleness,
            ))

        outcomes = await run(s.prometheus_namespace, True)
        live = _by_pod(outcomes.get(K_LIVE_PODS))
        live_outcome = outcomes.get(K_LIVE_PODS)

        # 降级 1：container_last_seen 缺失/报错 → 去掉新鲜度过滤重查（此时陈旧样本会短暂
        # 污染数值，所以必须留下 warning，不能静默）
        if live_outcome is not None and live_outcome.error:
            warnings.append("活 pod 查询报错，已退化为不过滤陈旧样本")
            outcomes[K_LIVE_PODS] = await self._single(
                build_queries(pod_regex, namespace=s.prometheus_namespace,
                              staleness_sec=s.prometheus_staleness_sec, use_staleness=False)[K_LIVE_PODS]
            )
            live = _by_pod(outcomes[K_LIVE_PODS])

        # 降级 2：namespace 配错 → 全部指标为空，去掉 namespace 重查
        if s.prometheus_namespace and not live:
            none_has_data = not any(_by_pod(o) for o in outcomes.values() if o is not None)
            if none_has_data:
                alt = await run("", True)
                if any(_by_pod(o) for o in alt.values() if o is not None):
                    warnings.append(
                        f"namespace={s.prometheus_namespace} 下无数据，已去掉 namespace 过滤重查"
                    )
                    outcomes = alt
                    live = _by_pod(outcomes[K_LIVE_PODS])

        live_pods = set(live)
        if not live_pods:
            # 兜底：新鲜度不可用但其它指标有数据时，用出现过的 pod 当活 pod
            for key, outcome in outcomes.items():
                if key == K_LIVE_PODS:
                    continue
                live_pods |= set(_by_pod(outcome))
            if live_pods:
                warnings.append("无 container_last_seen 数据，已用其它指标的 pod 集合代替")

        return outcomes, live_pods

    async def _single(self, expr: str) -> QueryOutcome:
        try:
            return QueryOutcome(key=K_LIVE_PODS, series=await self._client.query(expr))
        except Exception as exc:  # noqa: BLE001 — 降级路径不应再把异常抛出去
            return QueryOutcome(key=K_LIVE_PODS, error=str(exc))

    def _meta(self, discovered: int, pods_live: int, warnings: list[str]) -> dict:
        return {
            "source": "prometheus",
            "generatedAt": local_time_str(time.time()),
            "servicesDiscovered": discovered,
            "podsLive": pods_live,
            "warnings": warnings,
        }

    async def aclose(self) -> None:
        await self._client.aclose()


def build_app_indicators_service(settings: Settings) -> AppIndicatorsService:
    from .prometheus import build_prometheus_client
    from .service_meta import build_service_meta_source

    return AppIndicatorsService(
        build_prometheus_client(settings), settings, build_service_meta_source(settings)
    )
