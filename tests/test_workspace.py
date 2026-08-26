# -*- coding: utf-8 -*-
"""M3：WorkspaceManager —— base_sha 冻结 / 分支隔离 / 幂等 / 无 git_pull（design §8.7/§4.6）。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentflow.workspace.cmdb import MockCmdbProvider
from agentflow.workspace.manager import FrozenVersionMismatch, WorkspaceManager
from agentflow.workspace.models import RepoSpec


# ----------------------------------------------------------------------
# 测试基础设施：本地源仓库
# ----------------------------------------------------------------------
def git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout.strip()


def make_source_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    for fname, content in files.items():
        (repo / fname).parent.mkdir(parents=True, exist_ok=True)
        (repo / fname).write_text(content, encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def add_commit(repo: Path, fname: str, content: str) -> str:
    (repo / fname).parent.mkdir(parents=True, exist_ok=True)
    (repo / fname).write_text(content, encoding="utf-8")
    git("add", fname, cwd=repo)
    git("commit", "-q", "-m", f"update {fname}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def source_repos(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    order = make_source_repo(src, "order-service", {"pom.xml": "<order/>", "src/Order.java": "class Order {}"})
    warranty = make_source_repo(src, "warranty-service", {"pom.xml": "<warranty/>", "src/Warranty.java": "class Warranty {}"})
    return {"order-service": order, "warranty-service": warranty}


def file_url(repo: Path) -> str:
    return f"file://{repo}"


# ----------------------------------------------------------------------
# 场景 1：base_sha 冻结
# ----------------------------------------------------------------------
async def test_prepare_freezes_base_sha(source_repos: dict[str, Path], tmp_path: Path) -> None:
    order = source_repos["order-service"]
    base_sha = git("rev-parse", "HEAD", cwd=order)
    # 冻结在旧 commit，随后源仓库新增提交
    add_commit(order, "src/Order.java", "class Order { /* v2 */ }")

    wm = WorkspaceManager("team-alpha", "run_1", workspace_root=tmp_path / "ws")
    dest = await wm.prepare_one(RepoSpec("order-service", file_url(order), base_sha=base_sha))

    # 工作区停在冻结 SHA，不含新提交（§4.6 版本冻结）
    assert git("rev-parse", "HEAD", cwd=dest) == base_sha
    assert "v2" not in (dest / "src" / "Order.java").read_text()
    # AI 分支已创建
    assert git("branch", "--show-current", cwd=dest) == "aiops/RUN_run_1"


async def test_prepare_auto_resolve_remote_head(source_repos: dict[str, Path], tmp_path: Path) -> None:
    """base_sha 未指定 → 从远程 HEAD 冻结（§4.6）。"""
    order = source_repos["order-service"]
    wm = WorkspaceManager("team-alpha", "run_x", workspace_root=tmp_path / "ws")
    dest = await wm.prepare_one(RepoSpec("order-service", file_url(order)))
    expected = git("rev-parse", "HEAD", cwd=order)
    assert git("rev-parse", "HEAD", cwd=dest) == expected
    assert wm.frozen["order-service"].base_sha == expected


# ----------------------------------------------------------------------
# 场景 2：幂等 + 无 pull
# ----------------------------------------------------------------------
async def test_prepare_idempotent_no_reclone(source_repos: dict[str, Path], tmp_path: Path) -> None:
    order = source_repos["order-service"]
    base_sha = git("rev-parse", "HEAD", cwd=order)
    wm = WorkspaceManager("team-alpha", "run_i", workspace_root=tmp_path / "ws")
    d1 = await wm.prepare_one(RepoSpec("order-service", file_url(order), base_sha=base_sha))
    # 源仓库新增提交（模拟 run 期间远端前进）
    add_commit(order, "README.md", "new")
    # 再次 prepare 用同一 base_sha：幂等，不 pull、不 clone
    d2 = await wm.prepare_one(RepoSpec("order-service", file_url(order), base_sha=base_sha))
    assert d1 == d2
    assert git("rev-parse", "HEAD", cwd=d2) == base_sha
    assert not (d2 / "README.md").exists()  # 新提交未被拉取（§4.6 冻结）


async def test_frozen_version_mismatch_on_drift(source_repos: dict[str, Path], tmp_path: Path) -> None:
    """§4.6：run 期间工作区与 base_sha 漂移 → 报错，绝不 pull/reset。"""
    order = source_repos["order-service"]
    old_sha = git("rev-parse", "HEAD", cwd=order)
    wm = WorkspaceManager("team-alpha", "run_d", workspace_root=tmp_path / "ws")
    dest = await wm.prepare_one(RepoSpec("order-service", file_url(order), base_sha=old_sha))
    # 模拟 run 中工作区被外部改动
    git("reset", "--hard", "HEAD~0", cwd=dest)  # no-op，保持原状；改用强制移动到新 commit：
    git("commit", "--allow-empty", "-m", "drift", cwd=dest)
    with pytest.raises(FrozenVersionMismatch, match="版本冻结"):
        await wm.prepare_one(RepoSpec("order-service", file_url(order), base_sha=old_sha))


def test_no_git_pull_method(source_repos: dict[str, Path]) -> None:
    """§8.7.2 契约：WorkspaceManager 不提供 git_pull。"""
    wm = WorkspaceManager("t", "r", workspace_root=Path("/tmp"))
    assert not hasattr(wm, "pull")
    assert not hasattr(wm, "git_pull")


# ----------------------------------------------------------------------
# 场景 3：分支隔离（§8.7.3）
# ----------------------------------------------------------------------
async def test_concurrent_runs_branch_isolation(source_repos: dict[str, Path], tmp_path: Path) -> None:
    order = source_repos["order-service"]
    url = file_url(order)
    wm1 = WorkspaceManager("team-alpha", "run_1", workspace_root=tmp_path / "ws")
    wm2 = WorkspaceManager("team-alpha", "run_2", workspace_root=tmp_path / "ws")
    d1 = await wm1.prepare_one(RepoSpec("order-service", url))
    d2 = await wm2.prepare_one(RepoSpec("order-service", url))
    assert git("branch", "--show-current", cwd=d1) == "aiops/RUN_run_1"
    assert git("branch", "--show-current", cwd=d2) == "aiops/RUN_run_2"
    # 不同 Run 目录隔离
    assert d1 != d2


# ----------------------------------------------------------------------
# 场景 4：CMDB 驱动（§9.4）
# ----------------------------------------------------------------------
async def test_code_locator_cmdb_flow(source_repos: dict[str, Path], tmp_path: Path) -> None:
    """code-locator 定位流程：service → CMDB → RepoSpec → prepare。"""
    cmdb = MockCmdbProvider({"team-alpha": {"warranty-service": file_url(source_repos["warranty-service"])}})
    spec = await cmdb.get_repo_for_service("warranty-service")
    assert spec is not None and spec.service == "warranty-service"

    wm = WorkspaceManager("team-alpha", "run_c", workspace_root=tmp_path / "ws")
    dest = await wm.prepare_one(spec)
    assert (dest / "src" / "Warranty.java").exists()
    assert (await cmdb.get_services_for_tenant("team-alpha")) == ["warranty-service"]
