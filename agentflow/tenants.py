"""租户配置（design §9.3）：并发配额 + 审批人队列。

文件驱动（``AGENTFLOW_TENANTS_FILE`` 指向 YAML），缺省用内置默认（不限制——
dev 模式；生产必须显式配置）。格式：

.. code-block:: yaml

    default:
      max_concurrent_runs: 10
    tenants:
      team-alpha:
        max_concurrent_runs: 5
        approvers:
          approve-changes: ["alice@company.com"]

- ``approvers``：node_id → 审批人列表；未列出的审批节点回退租户级默认
  （``"*"`` 键），再无则不限制（dev）。审批人身份来自 JWT ``sub``（dev 模式
  为请求体 ``by``），授权依据始终是服务端配置，非客户端提交。
- 配额判定用 ``StateStore.count_active_runs``（未终态 run 都占名额）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TenantConfig:
    tenant_id: str
    max_concurrent_runs: int = 10
    # node_id → 审批人列表；"*" 为租户级默认；{} = 不限制（dev）
    approvers: dict[str, list[str]] = field(default_factory=dict)

    def approvers_for(self, node_id: str) -> list[str] | None:
        """该租户对某审批节点的审批人白名单；None = 不限制。"""
        if not self.approvers:
            return None
        return self.approvers.get(node_id, self.approvers.get("*"))


class TenantRegistry:
    """租户配置注册表：未知租户回退 default 配置。"""

    def __init__(self, default: TenantConfig, tenants: dict[str, TenantConfig]) -> None:
        self.default = default
        self.tenants = tenants

    @classmethod
    def builtin(cls) -> TenantRegistry:
        """无配置文件的缺省：配额 10、审批不限制（dev 模式）。"""
        return cls(default=TenantConfig(tenant_id="*"), tenants={})

    @classmethod
    def load(cls, path: str | Path) -> TenantRegistry:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        default_raw = raw.get("default") or {}
        default = TenantConfig(
            tenant_id="*",
            max_concurrent_runs=int(default_raw.get("max_concurrent_runs", 10)),
            approvers={
                k: [str(a) for a in v] for k, v in (default_raw.get("approvers") or {}).items()
            },
        )
        tenants: dict[str, TenantConfig] = {}
        for tid, t in (raw.get("tenants") or {}).items():
            tenants[str(tid)] = TenantConfig(
                tenant_id=str(tid),
                max_concurrent_runs=int((t or {}).get("max_concurrent_runs", default.max_concurrent_runs)),
                approvers={
                    k: [str(a) for a in v] for k, v in ((t or {}).get("approvers") or {}).items()
                },
            )
        return cls(default=default, tenants=tenants)

    def for_tenant(self, tenant_id: str) -> TenantConfig:
        return self.tenants.get(tenant_id, self.default)
