"""服务展示元数据：从 K8s Deployment 的 label 读 ``owner`` / ``type`` / ``agentName``。

这些字段**没有实测来源**（agentflow 无 CMDB），早先一律返回 ``null``。现在改为读
Deployment 上的声明式 label：既不是编造，也不用额外维护一份配置文件——改服务的人顺手就改了。

约定（前缀可配，默认 ``aiops``）::

    metadata:
      labels:
        aiops/type: sales          # 业务线；取值由使用方约定，本模块不做白名单校验
        aiops/owner: zhangsan      # 负责人
        aiops/agent: InspectAgent-1  # 巡检 agent（可选）

**失败绝不上升**：K8s 不可达 / 无权限 / 无 kubeconfig 时返回 ``(空字典, 原因)``，
调用方据此把这三个字段留空并往 ``meta.warnings`` 记一条——指标本身照常返回。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import Settings

logger = logging.getLogger("agentflow.datasource")

LABEL_PREFIX = "aiops"


class K8sServiceMetaSource:
    """按 service 名（= Deployment 名）读展示元数据。带短缓存。"""

    def __init__(
        self,
        namespace: str,
        *,
        enabled: bool = True,
        cache_ttl: float = 60.0,
        label_prefix: str = LABEL_PREFIX,
    ) -> None:
        self._namespace = namespace.strip()
        self._enabled = enabled
        self._cache_ttl = cache_ttl
        self._prefix = label_prefix
        self._apps = None
        self._cache: tuple[float, dict[str, dict]] | None = None

    @property
    def enabled(self) -> bool:
        # strip：配置里多打几个空格是常见手滑，不该被当成"配了 namespace"
        return self._enabled and bool(self._namespace.strip())

    async def fetch(self) -> tuple[dict[str, dict], str | None]:
        """返回 ``({service: {owner/type/agentName}}, 警告)``。**不抛异常。**"""
        if not self.enabled:
            return {}, None

        if self._cache_ttl > 0 and self._cache is not None:
            cached_at, cached = self._cache
            if time.monotonic() - cached_at < self._cache_ttl:
                return cached, None

        try:
            # kubernetes 客户端是同步阻塞的，必须挪出事件循环，否则会卡住整个 API
            data = await asyncio.to_thread(self._read_sync)
        except Exception as exc:  # noqa: BLE001 — 降级路径，任何异常都不该让整页 500
            logger.warning("读取 Deployment label 失败：%s", exc)
            return {}, f"K8s 标签读取失败（{type(exc).__name__}），owner/type 暂缺"

        if self._cache_ttl > 0:
            self._cache = (time.monotonic(), data)
        return data, None

    def _api(self):
        """惰性建 client。复用同一个 ApiClient 以复用连接池。"""
        if self._apps is None:
            from kubernetes import client
            from kubernetes import config as k8s_config

            try:
                k8s_config.load_incluster_config()   # 集群内（ServiceAccount）
            except Exception:  # noqa: BLE001 — 本地开发回落 kubeconfig，与 sandbox/tenantctl 同款
                k8s_config.load_kube_config()
            self._apps = client.AppsV1Api()
        return self._apps

    def _read_sync(self) -> dict[str, dict]:
        deps = self._api().list_namespaced_deployment(self._namespace)
        out: dict[str, dict] = {}
        for dep in deps.items or []:
            labels = ((dep.metadata.labels if dep.metadata else None) or {})
            meta = {
                "owner": labels.get(f"{self._prefix}/owner"),
                "type": labels.get(f"{self._prefix}/type"),
                "agentName": labels.get(f"{self._prefix}/agent"),
            }
            present = {k: v for k, v in meta.items() if v}
            # 只收有值的：没有 label 的 Deployment 不入表，调用方据此返回 null（而非空串）
            if present and dep.metadata:
                out[dep.metadata.name] = present
        return out

    def close(self) -> None:
        self._apps = None
        self._cache = None


def build_service_meta_source(settings: Settings) -> K8sServiceMetaSource:
    return K8sServiceMetaSource(
        settings.service_meta_namespace or settings.prometheus_namespace,
        enabled=settings.service_meta_enabled,
        cache_ttl=settings.service_meta_cache_ttl,
    )
