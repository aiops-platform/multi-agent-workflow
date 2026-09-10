"""真实数据源适配器（testbed 联调，design §7 数据源 / SCENARIOS §5）。

与 mock 工具**签名一致**（SCENARIOS §5.2）：query_logs / query_metrics /
check_infra / describe_pod。数据源切换只换 adapter，agent/工具名不变。

数据源端点（testbed port-forward）：
- Elasticsearch :19200（index `app-logs`，app.* 字段）
- Prometheus    :19090（/api/v1/query，cAdvisor+kubelet 采集）
- K8s           :kubectl（namespace `order`）
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..exec_context import current_node

log = logging.getLogger("agentflow.datasources")

ES_INDEX = "app-logs"


class DataSourceError(RuntimeError):
    pass


class RealDataSourceAdapter:
    def __init__(
        self,
        *,
        es_url: str = "http://localhost:19200",
        prom_url: str = "http://localhost:19090",
        namespace: str = "order",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.es_url = es_url.rstrip("/")
        self.prom_url = prom_url.rstrip("/")
        self.namespace = namespace
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        # 逃生舱（promql:/cadvisor:）按**节点**累计的调用数：键取自 ContextVar
        # current_node（executor 置位，asyncio task 隔离）→ 同一 adapter 实例被同波
        # 多节点并发调用时互不串扰；键为 ""（无执行上下文，如单测）时退化为全局计数。
        self._escape_calls: dict[str, int] = {}

    # ------------------------------------------------------------------
    # query_logs → ES（index app-logs，app.* 字段）
    # ------------------------------------------------------------------
    async def query_logs(
        self, service: str | None = None, level: str | None = None,
        trace_id: str | None = None, limit: int = 50,
    ) -> dict:
        filters = []
        if service:
            filters.append({"term": {"app.service.keyword": service}})
        if level:
            filters.append({"term": {"app.level.keyword": level.upper()}})
        if trace_id:
            # 真实 testbed 日志字段为 app.traceId（驼峰），兼容两种写法
            filters.append({
                "bool": {"should": [
                    {"term": {"app.traceId.keyword": trace_id}},
                    {"term": {"app.trace_id.keyword": trace_id}},
                ]}
            })
        body = {
            "size": min(limit, 200),
            "sort": [{"@timestamp": "desc"}],
            "query": {"bool": {"filter": filters}} if filters else {"match_all": {}},
        }
        resp = await self._client.post(
            f"{self.es_url}/{ES_INDEX}/_search", json=body
        )
        if resp.status_code >= 400:
            raise DataSourceError(f"ES 查询失败 ({resp.status_code}): {resp.text[:200]}")
        hits = resp.json().get("hits", {}).get("hits", [])
        logs = []
        for h in hits:
            s = h.get("_source", {})
            app = s.get("app", s)
            logs.append({
                "@timestamp": app.get("@timestamp", s.get("@timestamp")),
                "level": app.get("level"),
                "service": app.get("service"),
                "trace_id": app.get("trace_id") or app.get("traceId"),
                "message": app.get("message"),
            })
        return {"found": bool(logs), "count": len(logs), "logs": logs,
                "summary": f"ES 返回 {len(logs)} 条日志（service={service}, level={level}）"}

    # ------------------------------------------------------------------
    # get_trace → ES 按 traceId 关联重建调用链（SCENARIOS §3.6 / §6.6）
    # 真实 testbed 的 traceId 未必跨服务共享：支持按 traceId 查询；
    # 无 traceId 时回退为最近时间窗全链路日志。
    # ------------------------------------------------------------------
    async def get_trace(
        self, trace_id: str | None = None, service: str | None = None,
        minutes: int = 15, limit: int = 100,
    ) -> dict:
        filters: list[dict] = []
        if trace_id:
            filters.append({
                "bool": {"should": [
                    {"term": {"app.traceId.keyword": trace_id}},
                    {"term": {"app.trace_id.keyword": trace_id}},
                ]}
            })
        if service:
            filters.append({"term": {"app.service.keyword": service}})
        if not trace_id:
            filters.append({"range": {"@timestamp": {"gte": f"now-{minutes}m"}}})

        body = {
            "size": min(limit, 200),
            "sort": [{"@timestamp": "asc"}],
            "query": {"bool": {"filter": filters}} if filters else {"match_all": {}},
        }
        resp = await self._client.post(f"{self.es_url}/{ES_INDEX}/_search", json=body)
        if resp.status_code >= 400:
            raise DataSourceError(f"ES trace 查询失败 ({resp.status_code}): {resp.text[:200]}")
        hits = resp.json().get("hits", {}).get("hits", [])
        logs = []
        for h in hits:
            app = h.get("_source", {}).get("app", {})
            logs.append({
                "@timestamp": app.get("@timestamp"),
                "level": app.get("level"),
                "service": app.get("service"),
                "trace_id": app.get("traceId") or app.get("trace_id"),
                "message": (app.get("message") or "")[:200],
            })

        # 按 service 分组，重建调用链 + 判定故障 span
        spans: dict[str, list[dict]] = {}
        for l in logs:
            spans.setdefault(l["service"], []).append(l)

        chain = []
        failing = None
        for svc, slogs in spans.items():
            has_error = any(l["level"] == "ERROR" for l in slogs)
            completed = any(
                ("完成" in l["message"] or "成功" in l["message"]) for l in slogs
            )
            first_error = next((l["message"] for l in slogs if l["level"] == "ERROR"), None)
            span = {
                "service": svc, "spans": len(slogs),
                "has_error": has_error, "completed": completed,
                "first_error": first_error,
            }
            chain.append(span)

        # 判定故障 span：优先「错误非下游调用症状」的服务（根因），
        # 其次才是有 ERROR 未完成的服务。
        # 下游调用症状特征：feign / Read timed out / Connection refused / executing http
        def _is_downstream_symptom(span: dict) -> bool:
            err = (span.get("first_error") or "").lower()
            return any(k in err for k in (
                "feign", "read timed out", "connect timed out",
                "connection refused", "executing ", "could not connect",
            ))

        errored = [s for s in chain if s["has_error"] and not s["completed"]]
        origin = [s for s in errored if not _is_downstream_symptom(s)]
        if origin:
            failing = origin[0]["service"]
        elif errored:
            failing = errored[0]["service"]

        return {
            "trace_id": trace_id, "total": len(logs),
            "chain": chain, "failing_service": failing, "logs": logs[:20],
            "summary": f"trace 重建 {len(logs)} 条日志 / {len(chain)} 个服务"
                       + (f"，故障 span 疑似 {failing}" if failing else "，未见明显故障 span"),
        }

    # ------------------------------------------------------------------
    # query_metrics → Prometheus（cAdvisor/kubelet 采集）
    # ------------------------------------------------------------------
    async def query_metrics(self, service: str | None = None, metric: str | None = None) -> dict:
        pod_re = f"{service}.*" if service else ".*"
        # 逃生舱硬上限：promql:/cadvisor: 是「五个标准指标取不到数据时的下钻口」，
        # 不是探索工具。实测 metrics-analyst 用它逐字母枚举指标名跑了 54 次工具调用，
        # 轮次耗尽 → 整个节点无输出。软约束（prompt）不够，这里兜底。
        if metric and metric.startswith(("promql:", "cadvisor:")):
            key = current_node.get() or ""  # ContextVar：并行波次按节点隔离
            self._escape_calls[key] = self._escape_calls.get(key, 0) + 1
            if self._escape_calls[key] > _ESCAPE_HATCH_LIMIT:
                raise DataSourceError(
                    f"promql:/cadvisor: 逃生舱本节点已用 {self._escape_calls[key] - 1} 次"
                    f"（上限 {_ESCAPE_HATCH_LIMIT}）——请改用五个标准指标"
                    "（cpu_percent/memory_percent/disk_percent/error_rate/p95_latency_ms）"
                    "并立即基于已有数据输出结论；不要再用 promql: 做开放式探索。"
                )
        expr = _promql(metric, pod_re)
        resp = await self._client.get(f"{self.prom_url}/api/v1/query", params={"query": expr})
        if resp.status_code >= 400:
            raise DataSourceError(f"Prometheus 查询失败 ({resp.status_code}): {resp.text[:200]}")
        data = resp.json().get("data", {}).get("result", [])
        values = [float(r["value"][1]) for r in data if r.get("value")]
        return {
            "metric": metric or "raw",
            "expr": expr,
            "count": len(data),
            "value": sum(values) if values else None,
            "series": [{"labels": r.get("metric", {}), "value": r.get("value")} for r in data[:10]],
            "summary": f"Prometheus `{expr}` → {len(data)} 条序列"
                       + (f"，聚合值={sum(values):.2f}" if values else "（无数据）"),
        }

    # ------------------------------------------------------------------
    # check_infra / describe_pod → kubectl
    # ------------------------------------------------------------------
    async def check_infra(self, namespace: str | None = None, pod: str | None = None) -> dict:
        import json as _json

        ns = namespace or self.namespace
        if pod and _is_full_pod_name(pod):
            spec = pod
        else:
            label = ["-l", f"app={pod}"] if pod else []
            out = await _kubectl("get", "pods", "-n", ns, "-o", "json", *label)
            items = _json.loads(out).get("items", [])
            spec = items[0]["metadata"]["name"] if items else (pod or "")
        if not spec:
            return {"namespace": ns, "pod": None, "status": "NOT_FOUND", "summary": "未找到 pod"}
        out = await _kubectl("get", "pod", spec, "-n", ns, "-o", "json")
        d = _json.loads(out)
        md = d.get("metadata", {})
        st = d.get("status", {})
        restart_count = 0
        for cs in st.get("containerStatuses", []):
            restart_count += cs.get("restartCount", 0)
        return {
            "namespace": ns, "pod": md.get("name"), "status": st.get("phase"),
            "restarts": restart_count, "reason": st.get("reason"),
            "summary": f"pod {md.get('name')} status={st.get('phase')} restarts={restart_count}",
        }

    async def describe_pod(self, namespace: str | None = None, pod: str | None = None) -> dict:
        ns = namespace or self.namespace
        if not pod:
            return {"namespace": ns, "pod": None, "status": "NOT_FOUND"}
        out = await _kubectl("describe", "pod", pod, "-n", ns)
        return {"namespace": ns, "pod": pod, "status": "described", "describe": out[:2000]}

    async def aclose(self) -> None:
        await self._client.aclose()


# ----------------------------------------------------------------------
# PromQL 表达式映射（按指标名）
# ----------------------------------------------------------------------
# 容器 CPU 用量（cores）：与 _promql 的 cpu_limit 相除得占用百分比
_CPU_CORES = "sum(rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))"

# promql:/cadvisor: 逃生舱单节点上限（防开放式探索耗尽 ReAct 轮次）
_ESCAPE_HATCH_LIMIT = 3


def _promql(metric: str | None, pod_re: str) -> str:
    """metric 名 → PromQL 表达式。

    **未知 metric 一律抛 :class:`DataSourceError`，不再静默兜底**。

    此前的兜底是「未知 → 返回 CPU 使用率」：而 LLM 传的 5 个 metric 名
    （cpu_percent/memory_percent/disk_percent/error_rate/p95_latency_ms，取自
    ``MetricsEvidenceSchema``）**没有一个**命中旧白名单（旧键是 cpu/memory/disk…），
    于是五个查询全部落到同一条 CPU 表达式——metrics-analyst 拿到 5 个一模一样的
    数字，据此得出「CPU 0.5%，任务几乎空闲」的结论。真实数据 + 错误查询比假数据
    更危险：数字看着可信，语义完全错位。

    键名与 ``agents/schemas.py:MetricsEvidenceSchema`` 的字段保持一致（LLM 按 schema
    填 metric），并保留 cadvisor:/promql: 前缀作为逃生舱（可直接下钻任意表达式）。
    """
    if metric and metric.startswith(("cadvisor:", "promql:")):
        # 逃生舱：直接执行自定义 PromQL（绕过映射；仍走同一鉴权/异常路径）
        return metric.split(":", 1)[1]

    sel = f'pod=~"{pod_re}",container!="POD"'
    # 容器 CPU 上限（cores）= quota/period，用于把用量换算成百分比
    cpu_limit = (
        f'sum(container_spec_cpu_quota{{{sel}}}/container_spec_cpu_period{{{sel}}})'
    )
    queries = {
        # 与 MetricsEvidenceSchema 字段逐一对应
        "cpu_percent": (
            f"100 * {_CPU_CORES.format(sel=sel)} / {cpu_limit}"
        ),
        # 分母 >0 过滤：容器未设 memory limit 时 limit=0，直接相除得 +Inf（实测
        # order-service 即是），过滤后变「无数据」——比 Inf 诚实，避免 agent 把
        # 无穷大当成「内存爆了」。
        "memory_percent": (
            f'100 * sum(container_memory_working_set_bytes{{{sel}}})'
            f' / (sum(container_spec_memory_limit_bytes{{{sel}}}) > 0)'
        ),
        # 应用侧 data_disk_*（testbed 的 /data 盘）；total=0 时同样过滤为「无数据」
        "disk_percent": (
            f'100 * (1 - sum(data_disk_free_bytes{{service=~"{pod_re}"}})'
            f' / (sum(data_disk_total_bytes{{service=~"{pod_re}"}}) > 0))'
        ),
        # 5xx 占该服务总请求的比例（应用侧 http_server_requests_*，带 service 标签）
        "error_rate": (
            f'100 * sum(rate(http_server_requests_seconds_count{{service=~"{pod_re}",'
            f'status=~"5.."}}[5m]))'
            f' / sum(rate(http_server_requests_seconds_count{{service=~"{pod_re}"}}[5m]))'
        ),
        # P95 延迟（Spring 未开 histogram 时退化为 avg：_sum/_count，仍是真实数据）
        "p95_latency_ms": (
            f'1000 * sum(rate(http_server_requests_seconds_sum{{service=~"{pod_re}"}}[5m]))'
            f' / sum(rate(http_server_requests_seconds_count{{service=~"{pod_re}"}}[5m]))'
        ),
    }
    if metric in queries:
        return queries[metric]

    raise DataSourceError(
        f"未知 metric {metric!r}——不再静默兜底成 CPU（那会让不同指标返回同一个数字）。"
        f"可用：{', '.join(sorted(queries))}；"
        "或传 promql:<表达式> / cadvisor:<指标名> 直接下钻。"
    )


def _is_full_pod_name(name: str) -> bool:
    return bool(name) and name.count("-") >= 4


async def _kubectl(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "kubectl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DataSourceError(f"kubectl {' '.join(args)} 失败: {stderr.decode()[:300]}")
    return stdout.decode(errors="replace")
