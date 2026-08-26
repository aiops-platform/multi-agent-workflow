# -*- coding: utf-8 -*-
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
from typing import Any

import httpx

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
                ("完成" in l["message"] or "成功" in l["message"] or "成功" in l["message"])
                for l in slogs
            )
            first_error = next((l["message"] for l in slogs if l["level"] == "ERROR"), None)
            span = {
                "service": svc, "spans": len(slogs),
                "has_error": has_error, "completed": completed,
                "first_error": first_error,
            }
            chain.append(span)
            if has_error and not completed and failing is None:
                failing = svc

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
def _promql(metric: str | None, pod_re: str) -> str:
    sel = f'pod=~"{pod_re}",container!="POD"'
    queries = {
        "cpu": f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))",
        "memory": f"sum(container_memory_working_set_bytes{{{sel}}})",
        "disk": f'sum(container_fs_usage_bytes{{pod=~"{pod_re}"}})',
        "disk_limit": f'sum(container_fs_limit_bytes{{pod=~"{pod_re}"}})',
        "restarts": f"sum(kubelet_managed_container_restart_total) ",
    }
    if metric in queries:
        return queries[metric]
    if metric and metric.startswith("cadvisor:"):
        return metric[len("cadvisor:"):]
    # 默认：CPU 使用率（cores）
    return f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))"


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
