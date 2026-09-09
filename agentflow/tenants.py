"""租户配置（design-v5.3 §5.2/§9.3）。

v5.3 起**运行时以管理库为准**：``TenantRegistry.from_management`` 从 ManagementStore
构建；``tenants.yaml`` 降级为 bootstrap 种子（首启导入，:func:`bootstrap_tenants`）。
无管理库的 dev 场景回退 :meth:`TenantRegistry.builtin`（配额 10、审批不限制）。

审批人语义（§4.2 **default-deny**）：租户配置过任何 approvers（dict 非空）时，未命中
节点级白名单且无 ``"*"`` 通配 → **空列表 = 一律拒绝**（防"自建 workflow 换审批节点 id
绕过管控"）；``approvers`` 为空 dict 才是不限制（dev）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("agentflow.tenants")


@dataclass
class TenantConfig:
    tenant_id: str
    status: str = "active"  # provisioning | active | suspended | deleted
    isolation_level: str = "standard"  # strong | standard（P4/P5 分级）
    namespace: str = ""  # P3：K8s namespace；空 = agentflow-{tenant_id} 派生
    workers: int = 1  # P2：租户级 Worker 消费并发数
    max_concurrent_runs: int = 10
    approvers: dict[str, list[str]] = field(default_factory=dict)
    code_branch: str = "main"  # P5：部署分支（standard 固定 main）
    pinned_sha: str | None = None

    @property
    def effective_namespace(self) -> str:
        return self.namespace or f"agentflow-{self.tenant_id}"

    def approvers_for(self, node_id: str) -> list[str]:
        """该租户对某审批节点的审批人白名单；**空列表 = default-deny**（§4.2）。"""
        if not self.approvers:
            return []
        hit = self.approvers.get(node_id, self.approvers.get("*"))
        return list(hit) if hit else []


class TenantRegistry:
    """租户配置注册表：未知租户回退 default 配置（builtin 默认）。"""

    def __init__(self, default: TenantConfig, tenants: dict[str, TenantConfig]) -> None:
        self.default = default
        self.tenants = tenants

    @classmethod
    def builtin(cls) -> TenantRegistry:
        """无管理库的缺省：配额 10、审批不限制（dev 模式）。"""
        return cls(default=TenantConfig(tenant_id="*"), tenants={})

    @classmethod
    def from_rows(cls, rows: list[dict]) -> TenantRegistry:
        """从管理库 tenants 行构建（db_ref 等敏感列在行内忽略）。"""
        tenants = {
            r["tenant_id"]: TenantConfig(
                tenant_id=r["tenant_id"],
                status=r.get("status", "active"),
                isolation_level=r.get("isolation_level", "standard"),
                namespace=r.get("namespace", ""),
                workers=int(r.get("workers", 1)),
                max_concurrent_runs=int(r.get("max_concurrent_runs", 10)),
                approvers=dict(r.get("approvers") or {}),
                code_branch=r.get("code_branch", "main"),
                pinned_sha=r.get("pinned_sha"),
            )
            for r in rows
            if r.get("status") != "deleted"
        }
        default = TenantConfig(
            tenant_id="*",
            max_concurrent_runs=max((t.max_concurrent_runs for t in tenants.values()), default=10),
        )
        return cls(default=default, tenants=tenants)

    @classmethod
    async def from_management(cls, management) -> TenantRegistry:
        """从 ManagementStore 构建并缓存（CRUD 后重建，配置热生效）。"""
        return cls.from_rows(await management.list_tenants())

    def for_tenant(self, tenant_id: str) -> TenantConfig:
        cfg = self.tenants.get(tenant_id)
        if cfg is None:
            # 未注册租户：回退 default 配置（quota/approvers 继承），namespace 按 tenant_id 派生
            cfg = TenantConfig(
                tenant_id=tenant_id,
                isolation_level=self.default.isolation_level,
                workers=self.default.workers,
                max_concurrent_runs=self.default.max_concurrent_runs,
                approvers=dict(self.default.approvers),
                code_branch=self.default.code_branch,
            )
        return cfg

    def tenant_ids(self) -> list[str]:
        """注册租户清单（sweeper 遍历用）。"""
        return list(self.tenants)


# ----------------------------------------------------------------------
# bootstrap：tenants.yaml → 管理库（首启种子；运行时以管理库为准）
# ----------------------------------------------------------------------
async def async_bootstrap_tenants(management, settings) -> int:
    """tenants.yaml → 管理库种子导入（幂等：已存在同 id 租户跳过）。"""
    import json

    if not settings.tenants_file or not Path(settings.tenants_file).exists():
        return 0
    raw = yaml.safe_load(Path(settings.tenants_file).read_text(encoding="utf-8")) or {}
    existing = {r["tenant_id"] for r in await management.list_tenants()}
    imported = 0
    for tid, t in (raw.get("tenants") or {}).items():
        tid = str(tid)
        if tid in existing:
            continue
        t = t or {}
        db_ref = _default_db_ref(tid, settings)
        from .api.management_store import encrypt_db_ref

        await management.upsert_tenant({
            "tenant_id": tid,
            "status": "active",
            "isolation_level": str(t.get("isolation_level", "standard")),
            "namespace": str(t.get("namespace", f"agentflow-{tid}")),
            "workers": int(t.get("workers", 1)),
            "max_concurrent_runs": int(t.get("max_concurrent_runs", 10)),
            "approvers": {k: [str(a) for a in v] for k, v in (t.get("approvers") or {}).items()},
            "db_ref_enc": encrypt_db_ref(json.dumps(db_ref), settings),
            "code_branch": str(t.get("code_branch", "main")),
        })
        imported += 1
    if imported:
        log.info("bootstrap：已从 %s 导入 %d 个租户到管理库", settings.tenants_file, imported)
    return imported


def _default_db_ref(tenant_id: str, settings) -> dict:
    """bootstrap 未指定 db_ref 时的默认租户库引用（§5.4 分级策略）。"""
    from .config import postgres_dsn

    if settings.state_store == "postgres":
        return {"backend": "postgres", "dsn": postgres_dsn(settings)}
    return {
        "backend": "sqlite",
        "path": str(Path(settings.state_db_path).parent / "tenants" / f"{tenant_id}.db"),
    }


def parse_db_ref(raw: str) -> dict:
    import json

    return json.loads(raw)
