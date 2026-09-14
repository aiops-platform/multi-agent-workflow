"""tenantctl：租户生命周期 CLI（design-v5.3 §10，幂等 saga）。

    python -m agentflow.tenantctl provision <tenant> [--isolation strong|standard]
                                          [--workers N] [--quota N]
                                          [--branch b] [--namespace ns]
    python -m agentflow.tenantctl deploy   <tenant> --sha <sha> [--base-sha <main_sha>]
    python -m agentflow.tenantctl upgrade  <tenant> --sha <sha>     # = deploy
    python -m agentflow.tenantctl migrate  [<tenant>]               # 迁移扇出
    python -m agentflow.tenantctl deprovision <tenant> [--confirm-delete]

步骤（provision）：注册管理库（db_ref 加密）→ 建租户库（连接触发幂等建表）→
写 schema_versions → （best-effort）K8s namespace/ResourceQuota/NetworkPolicy/SA →
（kafka）建租户 topic。每步可重入；`deploy/upgrade` 记录 pin SHA / 镜像 tag /
部署时间（§9.2 规则 2：pin SHA 不 pin 分支名）。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .api.management_store import (
    build_management_store,
    encrypt_db_ref,
)
from .config import get_settings
from .statestore.router import TenantStoresRouter
from .tenants import (
    _default_db_ref,
    drop_tenant_database,
    ensure_tenant_database,
)

log = logging.getLogger("agentflow.tenantctl")

# 租户库 schema 版本（结构变更时递增；迁移扇出按 (tenant, pinned_sha) 记录）
SCHEMA_VERSION = "2026-09-09.1"


# 租户库引用的默认策略统一在 tenants._default_db_ref（router 回退路径与 provision
# 必须用同一套规则 —— 早期两处各写一份，postgres 分支都返回共享 DSN，隔离就是从那漏的）。


# ----------------------------------------------------------------------
# K8s namespace bootstrap（best-effort；未配置 kubeconfig 时跳过并提示）
# ----------------------------------------------------------------------
def ensure_namespace(namespace: str) -> bool:
    """创建租户 namespace + ResourceQuota + NetworkPolicy + SA（幂等）。

    返回是否执行（False = 无 kubeconfig，跳过——本地/POC 用 sqlite 时不需要）。"""
    try:
        from kubernetes import client, config
    except ImportError:  # pragma: no cover
        log.warning("kubernetes 包不可用，跳过 namespace bootstrap")
        return False
    try:
        config.load_incluster_config()
    except config.ConfigException:
        try:
            config.load_kube_config()
        except config.ConfigException:
            log.warning("无 kubeconfig，跳过 namespace bootstrap（%s）", namespace)
            return False

    v1 = client.CoreV1Api()
    try:
        v1.create_namespace(
            body=client.V1Namespace(metadata={"name": namespace})
        )
        log.info("已创建 namespace %s", namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 409:  # AlreadyExists → 幂等
            raise
        log.info("namespace %s 已存在（幂等跳过）", namespace)

    quota = client.V1ResourceQuota(
        metadata={"name": "agentflow-quota", "namespace": namespace},
        spec={
            "hard": {
                "limits.cpu": "8", "limits.memory": "16Gi",
                "pods": "20", "requests.cpu": "4", "requests.memory": "8Gi",
            }
        },
    )
    try:
        v1.create_namespaced_resource_quota(namespace=namespace, body=quota)
    except client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise

    # egress 白名单（§10.2）：默认拒绝全部出站，放行 DNS + LLM/MCP 域名由部署侧补
    networking = client.NetworkingV1Api()
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny-egress", "namespace": namespace},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [
                {"to": [{"namespaceSelector": {}}], "ports": [{"protocol": "UDP", "port": 53},
                                                              {"protocol": "TCP", "port": 53}]},
            ],
        },
    }
    try:
        networking.create_namespaced_network_policy(namespace=namespace, body=policy)
    except client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise

    try:
        v1.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(metadata={"name": "agentflow-worker"}),
        )
    except client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise
    return True


async def provision(args) -> int:
    settings = get_settings()
    mgmt = build_management_store(settings)
    await mgmt.connect()
    try:
        existing = await mgmt.get_tenant(args.tenant)
        if existing is not None and existing["status"] == "active" and not args.force:
            print(f"[tenantctl] 租户 {args.tenant} 已存在（status=active），幂等跳过（--force 重写）")
            return 0
        isolation = args.isolation or (existing or {}).get("isolation_level") or "standard"
        if args.branch and args.branch != "main" and isolation != "strong":
            print("[tenantctl] 治理规则 §9.2(4)：仅 strong 隔离租户允许专属分支，standard 固定 main")
            return 2
        # --db-dsn：strong 租户指向独立实例；不传则每租户一个 database（同实例）
        if getattr(args, "db_dsn", None):
            db_ref = {"backend": "postgres", "dsn": args.db_dsn}
        else:
            db_ref = _default_db_ref(args.tenant, settings)

        # 重新绑定已有租户时把旧库说清楚：数据不会自动搬，旧库仍留在原处
        if existing is not None:
            try:
                old = json.loads(decrypt(existing["db_ref_enc"], settings))
            except Exception:  # noqa: BLE001 —— 旧值解不开（换过 SECRET_KEY）不影响重绑
                old = None
            if old and old.get("dsn") and old["dsn"] != db_ref.get("dsn"):
                print(
                    f"[tenantctl] ⚠ 租户 {args.tenant} 的库引用已变更：\n"
                    f"            旧 → {old['dsn']}\n"
                    f"            新 → {db_ref.get('dsn')}\n"
                    f"            旧库数据**不会**自动搬迁，仍保留在原处。"
                )

        # 建库（幂等）：postgres 下每租户一个独立 database
        if await ensure_tenant_database(db_ref, settings):
            print(f"[tenantctl] 🆕 已创建租户库 {db_ref['dsn']}")

        await mgmt.upsert_tenant({
            "tenant_id": args.tenant,
            "status": "active",
            "isolation_level": isolation,
            "namespace": args.namespace or f"agentflow-{args.tenant}",
            "workers": args.workers,
            "max_concurrent_runs": args.quota,
            "approvers": {},
            "db_ref_enc": encrypt_db_ref(json.dumps(db_ref), settings),
            "code_branch": args.branch or "main",
            "pinned_sha": args.sha,
            "image_tag": f"{args.tenant}-{args.sha[:12]}" if args.sha else None,
            "created_at": (existing or {}).get("created_at"),
        })
        # 建租户库：连接触发幂等建表（StateStore + 三张配置表，同库异表）
        router = TenantStoresRouter(settings, mgmt)
        await router.get(args.tenant)
        await router.aclose()
        sha = args.sha or "init"
        await mgmt.set_schema_version(args.tenant, sha, SCHEMA_VERSION)
        if args.k8s:
            ensure_namespace(args.namespace or f"agentflow-{args.tenant}")
        print(
            f"[tenantctl] ✅ provision {args.tenant}: isolation={isolation} "
            f"db={db_ref.get('dsn') or db_ref.get('path')} "
            f"namespace={args.namespace or f'agentflow-{args.tenant}'} "
            f"schema={SCHEMA_VERSION}"
        )
        return 0
    finally:
        await mgmt.close()


async def deploy(args) -> int:
    """pin SHA → 记录镜像 tag/部署时间（滚动租户 Worker Deployment 由 CI/运维执行，
    此处打印对应 kubectl 提示；§9.2 规则 2：记录不可变 SHA）。"""
    settings = get_settings()
    mgmt = build_management_store(settings)
    await mgmt.connect()
    try:
        row = await mgmt.get_tenant(args.tenant)
        if row is None:
            print(f"[tenantctl] 租户 {args.tenant} 未开通，先 provision")
            return 1
        image_tag = f"{args.tenant}-{args.sha[:12]}"
        base = args.base_sha or row.get("main_base_sha")
        await mgmt.update_tenant(
            args.tenant,
            pinned_sha=args.sha,
            image_tag=image_tag,
            deployed_at=datetime.now(UTC).isoformat(),
            main_base_sha=base,
        )
        ns = row["namespace"]
        print(f"[tenantctl] ✅ deploy {args.tenant}: pin={args.sha[:12]} image={image_tag}")
        print(
            f"[tenantctl] 运维动作（CI/CD 承接）：kubectl -n {ns} set image "
            f"deployment/agentflow-worker agentflow=registry/agentflow:{image_tag} && "
            f"kubectl -n {ns} rollout status deployment/agentflow-worker"
        )
        return 0
    finally:
        await mgmt.close()


async def migrate(args) -> int:
    """迁移扇出（§5.5）：逐租户连库（幂等建表/补列）并记录 schema_versions。"""
    settings = get_settings()
    mgmt = build_management_store(settings)
    await mgmt.connect()
    try:
        tenants = [args.tenant] if args.tenant else [
            r["tenant_id"] for r in await mgmt.list_tenants(status="active")
        ]
        router = TenantStoresRouter(settings, mgmt)
        try:
            for tid in tenants:
                row = await mgmt.get_tenant(tid)
                sha = (row or {}).get("pinned_sha") or "init"
                await router.get(tid)  # 连接触发幂等 DDL
                await mgmt.set_schema_version(tid, sha, SCHEMA_VERSION)
                print(f"[tenantctl] ✅ migrate {tid} → {SCHEMA_VERSION} (sha={sha[:12]})")
        finally:
            await router.aclose()
        return 0
    finally:
        await mgmt.close()


async def deprovision(args) -> int:
    """注销：停用（status=deleted）；--confirm-delete 才删数据。

    - sqlite：删租户库文件
    - postgres：``DROP DATABASE ... WITH (FORCE)``（每租户一个库，见 ``tenants._default_db_ref``）
      —— 指向共享基础库的**旧配置会被拒绝**，避免把管理库连坐删掉。
    topics/namespace 清理由运维按 §10 逆向 saga 执行。
    """
    settings = get_settings()
    mgmt = build_management_store(settings)
    await mgmt.connect()
    try:
        row = await mgmt.get_tenant(args.tenant)
        if row is None:
            print(f"[tenantctl] 租户 {args.tenant} 不存在")
            return 1
        await mgmt.update_tenant(args.tenant, status="deleted")
        print(f"[tenantctl] ✅ deprovision {args.tenant}: status=deleted（Worker 消费循环热退出）")
        if args.confirm_delete:
            db_ref = json.loads(decrypt(row["db_ref_enc"], settings))
            if db_ref.get("backend") == "sqlite":
                Path(db_ref["path"]).unlink(missing_ok=True)
                print(f"[tenantctl] 🗑  已删除租户库文件 {db_ref['path']}")
            elif await drop_tenant_database(db_ref, settings):
                print(f"[tenantctl] 🗑  已删除租户库 {db_ref['dsn']}")
            else:
                print(f"[tenantctl] ⚠ 未删除租户库（指向共享基础库或不存在）：{db_ref.get('dsn')}")
        return 0
    finally:
        await mgmt.close()


def decrypt(stored: str, settings) -> str:
    from .api.management_store import decrypt_db_ref

    return decrypt_db_ref(stored, settings)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tenantctl", description="agentflow 租户生命周期 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("provision", help="开通租户（幂等）")
    p.add_argument("tenant")
    p.add_argument(
        "--db-dsn",
        default=None,
        help="指定租户库 DSN（strong 隔离指向独立实例时用）。"
        "不传则在同一实例上建独立 database：{基础库}-{tenant}",
    )
    p.add_argument("--isolation", choices=["strong", "standard"], default=None)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--quota", type=int, default=10, help="max_concurrent_runs")
    p.add_argument("--branch", default=None, help="部署分支（standard 固定 main，§9.2 规则 4）")
    p.add_argument("--namespace", default=None)
    p.add_argument("--sha", default=None, help="初始 pin SHA")
    p.add_argument("--k8s", action="store_true", help="best-effort 创建 K8s namespace/RQ/NP/SA")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("deploy", help="部署/升级（pin SHA，幂等记录）")
    p.add_argument("tenant")
    p.add_argument("--sha", required=True)
    p.add_argument("--base-sha", default=None, help="漂移追踪基准（main SHA）")

    p = sub.add_parser("upgrade", help="= deploy（别名，语义清晰）")
    p.add_argument("tenant")
    p.add_argument("--sha", required=True)
    p.add_argument("--base-sha", default=None)

    p = sub.add_parser("migrate", help="迁移扇出（逐租户幂等 DDL + schema_versions）")
    p.add_argument("tenant", nargs="?")

    p = sub.add_parser("deprovision", help="注销租户")
    p.add_argument("tenant")
    p.add_argument("--confirm-delete", action="store_true", help="确认删除租户库数据")
    return ap


async def run_async(argv: list[str]) -> int:
    """程序化入口（测试/编排可在事件循环内 await）。"""
    args = _build_parser().parse_args(argv)
    handler = {
        "provision": provision, "deploy": deploy, "upgrade": deploy,
        "migrate": migrate, "deprovision": deprovision,
    }[args.cmd]
    return await handler(args)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(run_async(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
