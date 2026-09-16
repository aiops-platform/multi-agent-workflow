"""Deployment label → 展示元数据（owner/type/agentName）。

无集群：monkeypatch ``_api`` 返回假 AppsV1Api。重点是**失败绝不上升**——
K8s 不可达时 owner/type 该留空并给出原因，而不是让整个 /app-indicators 挂掉。
"""
from types import SimpleNamespace

import pytest

from agentflow.config import Settings
from agentflow.datasource.service_meta import (
    K8sServiceMetaSource,
    build_service_meta_source,
)


def _dep(name: str, labels: dict | None):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels))


class _FakeApps:
    def __init__(self, deps) -> None:
        self._deps = deps
        self.calls = 0

    def list_namespaced_deployment(self, namespace):
        self.calls += 1
        return SimpleNamespace(items=self._deps)


def _source(deps, **over) -> tuple[K8sServiceMetaSource, _FakeApps]:
    src = K8sServiceMetaSource("order", **{"cache_ttl": 0.0, **over})
    fake = _FakeApps(deps)
    src._api = lambda: fake          # 绕开真实 kubeconfig 加载
    return src, fake


async def test_maps_all_three_labels() -> None:
    src, _ = _source([
        _dep("order-service", {"app": "order-service", "aiops/type": "sales", "aiops/owner": "h.a.hu"}),
        _dep("warranty-service", {"aiops/type": "aftersales", "aiops/owner": "bo.gong",
                                  "aiops/agent": "InspectAgent-1"}),
    ])
    meta, warning = await src.fetch()
    assert warning is None
    assert meta["order-service"] == {"type": "sales", "owner": "h.a.hu"}
    assert meta["warranty-service"]["agentName"] == "InspectAgent-1"


async def test_deployments_without_aiops_labels_are_omitted() -> None:
    """没打 label 的 Deployment 不进表，调用方据此返回 null（而不是空串）。"""
    src, _ = _source([
        _dep("gateway-service", {"app": "gateway-service", "aiops/type": "gateway"}),
        _dep("prometheus", {"app": "prometheus"}),
        _dep("no-labels", None),
    ])
    meta, _ = await src.fetch()
    assert meta == {"gateway-service": {"type": "gateway"}}
    assert "prometheus" not in meta and "no-labels" not in meta


async def test_empty_label_values_are_dropped() -> None:
    src, _ = _source([_dep("svc", {"aiops/owner": "", "aiops/type": "sales"})])
    meta, _ = await src.fetch()
    assert meta == {"svc": {"type": "sales"}}      # 空串当成没配


async def test_custom_prefix() -> None:
    src, _ = _source([_dep("svc", {"sip/owner": "someone"})], label_prefix="sip")
    meta, _ = await src.fetch()
    assert meta == {"svc": {"owner": "someone"}}


async def test_k8s_failure_returns_warning_not_raise() -> None:
    """**关键回归**：K8s 不可达不能把 /app-indicators 拖挂。"""
    src = K8sServiceMetaSource("order", cache_ttl=0.0)

    def boom():
        raise RuntimeError("no kubeconfig")

    src._api = boom
    meta, warning = await src.fetch()
    assert meta == {}
    assert warning and "K8s 标签读取失败" in warning


async def test_cache_avoids_repeated_k8s_calls() -> None:
    src, fake = _source([_dep("svc", {"aiops/type": "sales"})], cache_ttl=60.0)
    await src.fetch()
    await src.fetch()
    assert fake.calls == 1

    src2, fake2 = _source([_dep("svc", {"aiops/type": "sales"})], cache_ttl=0.0)
    await src2.fetch()
    await src2.fetch()
    assert fake2.calls == 2


async def test_disabled_skips_k8s_entirely() -> None:
    src, fake = _source([_dep("svc", {"aiops/type": "sales"})], enabled=False)
    meta, warning = await src.fetch()
    assert (meta, warning) == ({}, None)
    assert fake.calls == 0


@pytest.mark.parametrize("namespace", ["", "   "])
async def test_blank_namespace_is_disabled(namespace) -> None:
    src = K8sServiceMetaSource(namespace)
    assert src.enabled is False


def test_factory_falls_back_to_prometheus_namespace() -> None:
    a = build_service_meta_source(Settings(service_meta_namespace="", prometheus_namespace="order"))
    assert a.enabled is True
    b = build_service_meta_source(Settings(service_meta_namespace="custom", prometheus_namespace="order"))
    assert b._namespace == "custom"
    c = build_service_meta_source(Settings(service_meta_enabled=False))
    assert c.enabled is False
