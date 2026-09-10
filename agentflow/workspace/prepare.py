"""Run 级工作区准备（§8.7.2）：run 创建后、发布 trigger 前，把本次 run 需要的仓库
克隆到 ``{workspace_root}/{tenant}/{run}/repos/{service}`` 的 ``aiops/RUN_{run_id}`` 分支。

**必须在 worker 消费 trigger 之前完成**——修复侧 agent 的工作区工具
（``agents/workspace_tools.py``）按 ``current_run`` 定位工作区，工作区不存在即工具报错。
因此接线点在 ``RunService._create()``（queue 发布 trigger 之前），而不是节点执行期。

仓库来源（按优先级）：
1. ``inputs.repos``：``{service: url}`` 显式映射（**加固姿态下被封堵**，见
   ``_create`` 的 InputsValidationError——这在此处只作 dev 联调兜底）；
2. 租户 CMDB（``MockCmdbProvider``）：由诊断段 trace 定位到的 service 反查 repo。

两个来源都拿不到 → 本次 run **不准备工作区**，修复侧 agent 的工具会在使用时
明确报错（fail-closed，不静默跑成 mock）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .manager import GitShell, WorkspaceManager
from .models import RepoSpec

log = logging.getLogger("agentflow.workspace.prepare")

# 默认本地 testbed 仓库根（dev 联调：file:// 源，无需网络）
DEFAULT_REPO_ROOT = Path("/Users/bo.gong/Documents/accenture/workspace/agentflow-testbed/services")


def default_cmdb() -> dict[str, dict[str, str]]:
    """本地 testbed 的 tenant → {service: repo url} 映射（file:// 本地源）。

    生产由真实 CMDB 取代（§9.4 TenantMappingProvider）。
    """
    root = DEFAULT_REPO_ROOT
    services = {
        "order-service": f"file://{root}/aiops-test-order-service",
        "warranty-service": f"file://{root}/aiops-test-warranty-service",
        "gateway-service": f"file://{root}/aiops-test-gateway-service",
    }
    return {"local": dict(services), "team-alpha": dict(services)}


async def prepare_run_workspace(
    tenant_id: str,
    run_id: str,
    *,
    workspace_root: Path,
    inputs: dict | None = None,
    services: list[str] | None = None,
    cmdb_map: dict[str, dict[str, str]] | None = None,
    git: GitShell | None = None,
) -> list[Path]:
    """准备本次 run 的工作区，返回已准备的仓库路径列表（空 = 无需工作区）。

    ``services``：需要准备的 service 列表（诊断段定位结果；无则用 CMDB 全量，
    但全量克隆代价高——调用方应尽量传入 trace 定位到的服务）。
    """
    mapping = (cmdb_map or default_cmdb()).get(tenant_id, {})
    explicit = (inputs or {}).get("repos") or {}

    targets: dict[str, str] = {}
    # services 为空 → 准备该租户 CMDB 的全部仓库：修复侧要改哪个服务在 run 创建时
    # 尚未确定（诊断段才定位），而工作区必须在 trigger 之前就绪（见模块 docstring）。
    for name in (services or list(mapping)):
        if name in explicit:
            targets[name] = explicit[name]
        elif name in mapping:
            targets[name] = mapping[name]
    # 显式 repos 里未被 services 提到的也一并准备（dev 联调直接指定仓库的场景）
    for name, url in explicit.items():
        targets.setdefault(name, url)

    if not targets:
        log.info("[%s] 无可准备仓库（services=%s，CMDB 未命中）——跳过工作区", run_id, services)
        return []

    wm = WorkspaceManager(tenant_id, run_id, workspace_root=workspace_root, git=git)
    paths: list[Path] = []
    for service, url in targets.items():
        try:
            paths.append(await wm.prepare_one(RepoSpec(service=service, url=url)))
        except Exception as exc:  # noqa: BLE001 - 单个仓库失败不阻断其它仓库
            log.warning("[%s] 准备 %s 工作区失败: %s", run_id, service, exc)
    log.info("[%s] 工作区就绪 %d 个仓库（分支 %s）", run_id, len(paths), wm.run_branch)
    return paths
