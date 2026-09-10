# -*- coding: utf-8 -*-
"""租户数据源映射（design §9.4 TenantMappingProvider）。

code-locator 定位流程：
``trace 输出 failing_service → get_repo_for_service → RepoSpec → WorkspaceManager.prepare``

生产实现接真实 CMDB + 拓扑；本地 MVP 用 ``MockCmdbProvider``（配置驱动，不依赖外部）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import RepoSpec


class TenantMappingProvider(ABC):
    @abstractmethod
    async def get_services_for_tenant(self, tenant_id: str) -> list[str]:
        """该租户拥有哪些 service（CMDB 查询）。"""

    @abstractmethod
    async def get_repo_for_service(self, service: str) -> RepoSpec | None:
        """service 对应的 repo（CMDB + 拓扑查询）。查不到返回 None。"""


class MockCmdbProvider(TenantMappingProvider):
    """内存 CMDB：tenant → {service → url}。本地联调默认实现。"""

    def __init__(self, mapping: dict[str, dict[str, str]] | None = None) -> None:
        # tenant_id -> {service -> remote_url}
        self._mapping: dict[str, dict[str, str]] = mapping or {
            "team-alpha": {
                "order-service": "https://github.com/company/order-service",
                "warranty-service": "https://github.com/company/warranty-service",
                "gateway-service": "https://github.com/company/gateway-service",
            }
        }

    def register(self, tenant_id: str, service: str, url: str) -> None:
        self._mapping.setdefault(tenant_id, {})[service] = url

    async def get_services_for_tenant(self, tenant_id: str) -> list[str]:
        return list(self._mapping.get(tenant_id, {}))

    async def get_repo_for_service(self, service: str) -> RepoSpec | None:
        for services in self._mapping.values():
            if service in services:
                return RepoSpec(service=service, url=services[service])
        return None
