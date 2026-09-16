"""Prometheus HTTP 客户端（薄封装：查询 + JSON 解析 + 并发）。

**本模块零 FastAPI 依赖、零业务语义**——PromQL 由调用方（``app_indicators.py``）构造，
这里只负责把 ``/api/v1/query`` 的结果变成 ``Series`` 列表。

⚠️ 架构例外见 ``datasource/__init__.py``。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from ..config import Settings

logger = logging.getLogger("agentflow.datasource")


class DataSourceError(RuntimeError):
    """Prometheus 不可达 / 返回非 success / 响应格式非法。"""


@dataclass(frozen=True)
class Series:
    """一条瞬时向量样本。``ts`` 用 Prometheus 侧时间戳，不用宿主时钟。"""

    metric: dict[str, str]
    value: float
    ts: float


@dataclass
class QueryOutcome:
    """``query_many`` 的单条结果：失败不抛，收敛成 error 字段供上层降级。"""

    key: str
    series: list[Series] = field(default_factory=list)
    error: str | None = None


class PrometheusClient:
    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                # connect 单独给短超时：Prometheus 没起时快速失败，不让页面等满 read 超时
                timeout=httpx.Timeout(connect=2.0, read=self._timeout, write=2.0, pool=2.0),
            )
        return self._client

    async def query(self, expr: str) -> list[Series]:
        """执行一次瞬时查询。失败抛 ``DataSourceError``。"""
        client = self._ensure_client()
        try:
            resp = await client.get("/api/v1/query", params={"query": expr})
        except httpx.HTTPError as exc:
            # 不回显 base_url（沿用仓库「不回显内部地址」惯例）
            raise DataSourceError(f"Prometheus 不可达（{type(exc).__name__}）") from exc

        if resp.status_code != 200:
            # 带上 Prometheus 的错误正文：400 多为 PromQL 解析失败（如 RE2 不认的转义），
            # 只报状态码会让排查变成猜谜。截断以免超长查询语句刷屏。
            raise DataSourceError(
                f"Prometheus 返回 HTTP {resp.status_code}：{resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataSourceError("Prometheus 响应不是合法 JSON") from exc

        if payload.get("status") != "success":
            raise DataSourceError(f"Prometheus 查询失败：{payload.get('error') or 'unknown'}")

        result = (payload.get("data") or {}).get("result") or []
        out: list[Series] = []
        for item in result:
            pair = item.get("value")
            # 只接受瞬时向量（matrix 形状没有 value 键，直接跳过）
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                value = float(pair[1])
                ts = float(pair[0])
            except (TypeError, ValueError):
                continue
            out.append(Series(metric=item.get("metric") or {}, value=value, ts=ts))
        return out

    async def query_many(self, exprs: Mapping[str, str]) -> dict[str, QueryOutcome]:
        """并发执行多条查询。单条失败只标记该条 error，不抛——避免一个指标拖垮整页。"""
        keys = list(exprs)

        async def run(key: str) -> QueryOutcome:
            try:
                return QueryOutcome(key=key, series=await self.query(exprs[key]))
            except DataSourceError as exc:
                return QueryOutcome(key=key, error=str(exc))

        results = await asyncio.gather(*(run(k) for k in keys))
        return {r.key: r for r in results}

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def build_prometheus_client(settings: Settings) -> PrometheusClient:
    return PrometheusClient(
        settings.prometheus_url, timeout=settings.prometheus_timeout_sec
    )
