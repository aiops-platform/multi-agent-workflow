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
    """租户库引用默认策略（§5.4 分级策略）。

    **两个后端都是「每租户一份」**：
    - sqlite：``data/tenants/{tenant_id}.db``
    - postgres：**同名实例上的独立 database**，命名 ``{基础库}-{tenant_id}``
      （与 K8s namespace ``agentflow-{tenant}`` 同一套命名）

    postgres 分支早期返回的是**共享 DSN**（所有租户指向同一个库）。后果不是"少建了个库"
    这么简单：``workflows`` / ``mcp_servers`` / ``agent_configs`` 三张控制面表**没有
    tenant_id 列**（设计上依赖"每租户一个库"的物理隔离），共享库下它们就成了跨租户共享 ——
    实测用一个从未开通的租户 id 就能列出别人的 workflow。改为每租户独立库后，
    这三张表天然落在各自的库里，物理隔离成立。

    > ``isolation_level`` 不参与本函数：``strong``（"独立实例"）需要另配 DSN，
    > 目前两种隔离都落在**同一个 PG 实例的不同 database** 上。真正的独立实例
    > 请用 ``tenantctl provision --db-dsn`` 显式指定。
    """
    from .config import postgres_dsn

    if settings.state_store == "postgres":
        base = postgres_dsn(settings)
        return {"backend": "postgres", "dsn": dsn_with_db(base, tenant_db_name(base, tenant_id))}
    return {
        "backend": "sqlite",
        "path": str(Path(settings.state_db_path).parent / "tenants" / f"{tenant_id}.db"),
    }


def db_name_of(dsn: str) -> str:
    """取 DSN 里的库名（无则空串）。"""
    from urllib.parse import urlsplit

    return urlsplit(dsn).path.lstrip("/")


def dsn_with_db(dsn: str, dbname: str) -> str:
    """换库名。

    **用 urlsplit/urlunsplit 而不是字符串拼接**：本仓的 DSN 把凭据放在 **query** 里
    （``postgresql://host:5432/agentflow?user=…&password=…``）。早期用
    ``prefix + "/" + name`` 把库名拼到了 query **之后**，得到一个畸形连接串 ——
    实测报 ``password authentication failed``（整串被当作密码解析），
    排查方向完全指错。urlunsplit 保证路径在 query 之前。
    """
    from urllib.parse import urlsplit, urlunsplit

    u = urlsplit(dsn)
    return urlunsplit((u.scheme, u.netloc, "/" + dbname, u.query, u.fragment))


def tenant_db_name(base_dsn: str, tenant_id: str) -> str:
    """基础 DSN → 该租户的库名（``{基础库}-{tenant_id}``）。

    PG 标识符上限 63 字节，超了直接报错而不是静默截断 —— 截断会让两个长租户名
    撞到同一个库，又变回共享。
    """
    base_db = db_name_of(base_dsn)
    name = f"{base_db}-{tenant_id}"
    if len(name.encode()) > 63:
        raise ValueError(
            f"租户 {tenant_id!r} 派生的库名超过 PostgreSQL 63 字节上限：{name!r}"
        )
    return name


async def ensure_tenant_database(db_ref: dict, settings) -> bool:
    """确保 PG 租户库存在（不存在则 ``CREATE DATABASE``）。返回是否新建。

    - 非 postgres / 目标是共享基础库（旧配置）→ 不做任何事，返回 False
    - 连**基础库**做管理操作（目标库还不存在，连不上它）；``CREATE DATABASE``
      不能在事务里跑，故用 autocommit
    """
    if db_ref.get("backend") != "postgres":
        return False

    from .config import postgres_dsn

    dsn = db_ref["dsn"]
    target = db_name_of(dsn)
    base_dsn = postgres_dsn(settings)
    base = db_name_of(base_dsn)
    if not target or target == base:
        return False  # 旧配置：指向共享基础库，无事可做

    import psycopg
    from psycopg import sql

    admin_dsn = dsn_with_db(dsn, base)
    conn = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
        if await cur.fetchone():
            return False
        # 标识符不能参数化，用 Identifier 转义（库名含 '-' 等字符）。
        # **必须显式 TEMPLATE template0**：默认模板 template1 **允许连接**，只要有人
        # （哪怕是个忘了关的 psql）连着它，CREATE DATABASE 就会
        # `ObjectInUse: source database "template1" is being accessed by other users` 失败。
        # template0 从不接受连接，是脚本化建库的标准模板。
        await conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target))
        )
        log.info("已创建租户库 %s", target)
        return True
    finally:
        await conn.close()


async def drop_tenant_database(db_ref: dict, settings) -> bool:
    """删除 PG 租户库（deprovision 用）。返回是否真的删了。

    **拒绝删基础库**：旧配置下所有租户的 db_ref 都指向共享基础库，
    若无脑 DROP 会把管理库连同所有租户数据一起抹掉。
    """
    if db_ref.get("backend") != "postgres":
        return False

    from .config import postgres_dsn

    dsn = db_ref["dsn"]
    target = db_name_of(dsn)
    base_dsn = postgres_dsn(settings)
    base = db_name_of(base_dsn)
    if not target or target == base:
        log.warning("租户库指向共享基础库 %s，拒绝删除", base)
        return False

    import psycopg
    from psycopg import sql

    admin_dsn = dsn_with_db(dsn, base)
    conn = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
        if not await cur.fetchone():
            return False
        # WITH (FORCE) 踢掉残留连接（PG 13+）
        await conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(target))
        )
        log.info("已删除租户库 %s", target)
        return True
    finally:
        await conn.close()


def parse_db_ref(raw: str) -> dict:
    import json

    return json.loads(raw)
