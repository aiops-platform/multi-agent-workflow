"""Run 级工作区准备（§8.7.2）：run 创建后、发布 trigger 前，把本次 run 需要的仓库
克隆到 ``{workspace_root}/{tenant}/{run}/repos/{service}`` 的 ``aiops/RUN_{run_id}`` 分支。

**必须在 worker 消费 trigger 之前完成**——修复侧 agent 的工作区工具
（``agents/workspace_tools.py``）按 ``current_run`` 定位工作区，工作区不存在即工具报错。
因此接线点在 ``RunService._create()``（queue 发布 trigger 之前），而不是节点执行期。

仓库来源（按优先级）：
1. ``inputs.repos``：``{service: url}`` 显式映射（**加固姿态下被封堵**，见
   ``_create`` 的 InputsValidationError——这在此处只作 dev 联调兜底）；
2. **部署配置**（``AGENTFLOW_REPO_ROOT`` + ``AGENTFLOW_REPO_MAP``，见 ``configured_repos()``）。

> **为什么不走 MCP 的 CMDB**：工作区准备发生在 ``RunService._create``——**run 创建期，
> 早于任何 agent 节点执行**，此时还没有 MCP 调用可言。agent 侧的 CMDB 查询（``locate_repo``
> / ``get_service_topology``）走 MCP；本文件只负责"把仓库拉到位"这件基建动作，
> 用部署配置驱动。

两个来源都拿不到 → 本次 run **不准备工作区**，修复侧 agent 的工具会在使用时
明确报错（fail-closed，不静默跑成 mock）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import get_settings
from .manager import GitShell, WorkspaceManager
from .models import RepoSpec

log = logging.getLogger("agentflow.workspace.prepare")

def configured_repos() -> dict[str, str]:
    """部署配置的 service → repo URL 映射（**不再硬编码任何本机路径**）。

    - ``AGENTFLOW_REPO_ROOT`` 为空 → 返回空（调用方跳过准备并记录原因）；
    - ``AGENTFLOW_REPO_MAP``：JSON ``{"service": "仓库目录名"}``，缺省时目录名取服务名。
      例（testbed）：``{"order-service": "aiops-test-order-service", ...}``
    - 支持的 URL 形式：``/abs/path`` 或 ``file:///abs/path``（本地源）、``https://…``（远端）。

    早期版本这里硬编码了某台开发机的绝对路径——换台机器就静默失效，已移除。
    """
    settings = get_settings()
    root = (settings.repo_root or "").strip()
    if not root:
        return {}
    try:
        overrides = json.loads(settings.repo_map) if settings.repo_map.strip() else {}
    except json.JSONDecodeError as exc:
        log.warning("AGENTFLOW_REPO_MAP 不是合法 JSON，忽略覆盖：%s", exc)
        overrides = {}
    if not isinstance(overrides, dict):
        log.warning("AGENTFLOW_REPO_MAP 应为对象，忽略：%r", overrides)
        overrides = {}

    base = root.rstrip("/")
    return {
        service: (f"{base}/{dirname}" if base.startswith(("http://", "https://", "file://"))
                  else f"file://{base}/{dirname}")
        for service, dirname in overrides.items()
    } if overrides else {}


async def prepare_run_workspace(
    tenant_id: str,
    run_id: str,
    *,
    workspace_root: Path,
    inputs: dict | None = None,
    services: list[str] | None = None,
    repo_map: dict[str, str] | None = None,
    git: GitShell | None = None,
) -> list[Path]:
    """准备本次 run 的工作区，返回已准备的仓库路径列表（空 = 无需工作区）。

    ``repo_map``：service → repo URL 的映射；缺省取 ``configured_repos()``（部署配置）。
    ``services``：**只准备这些服务**；为空则准备 ``repo_map`` 的全部——修复侧要改哪个
    服务在 run 创建时尚未确定（诊断段才定位），而工作区必须在 trigger 之前就绪
    （见模块 docstring），故默认全量。
    """
    mapping = configured_repos() if repo_map is None else repo_map
    explicit = (inputs or {}).get("repos") or {}

    targets: dict[str, str] = {}
    for name in (services or list(mapping)):
        if name in explicit:
            targets[name] = explicit[name]
        elif name in mapping:
            targets[name] = mapping[name]
    # 显式 repos 里未被 services 提到的也一并准备（dev 联调直接指定仓库的场景）
    for name, url in explicit.items():
        targets.setdefault(name, url)

    if not targets:
        if not mapping:
            log.warning(
                "[%s] 未配置仓库映射（AGENTFLOW_REPO_ROOT 为空）——跳过工作区准备。"
                "修复侧 agent 的工作区工具将报错。", run_id
            )
        else:
            log.info("[%s] 无可准备仓库（services=%s）——跳过工作区", run_id, services)
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
