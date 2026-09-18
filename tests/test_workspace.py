"""M3：WorkspaceManager —— base_sha 冻结 / 分支隔离 / 幂等 / 无 git_pull（design §8.7/§4.6）。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentflow.workspace.manager import FrozenVersionMismatch, WorkspaceManager
from agentflow.workspace.models import RepoSpec


# ----------------------------------------------------------------------
# 测试基础设施：本地源仓库
# ----------------------------------------------------------------------
def git(*args: str, cwd: Path | None = None) -> str:
    # check=False：返回码由下面的 assert 显式判定（要读到 stderr 才能给出可读错因）
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
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
# 场景 4：仓库映射来自**部署配置**（不再硬编码路径、不再走本地 CMDB）
# ----------------------------------------------------------------------
async def test_repo_map_from_config(source_repos: dict[str, Path], tmp_path: Path) -> None:
    """service → repo URL 由 repo_map 提供 → prepare 到工作区。

    背景：此前的 `MockCmdbProvider` / `default_cmdb()` 把**某台开发机的绝对路径**
    硬编码在生产路径里（换机器即静默失效），已删除；CMDB 查询迁至 MCP
    （`locate_repo` / `get_service_topology`），工作区准备改由部署配置驱动。
    """
    repo_map = {"warranty-service": file_url(source_repos["warranty-service"])}
    wm = WorkspaceManager("team-alpha", "run_c", workspace_root=tmp_path / "ws")
    dest = await wm.prepare_one(
        RepoSpec(service="warranty-service", url=repo_map["warranty-service"])
    )
    assert (dest / "src" / "Warranty.java").exists()


def test_configured_repos_empty_without_root() -> None:
    """未配 repo_root → 空映射（宁可跳过，也不用错路径）。"""
    from agentflow.workspace.prepare import configured_repos

    assert configured_repos() == {}   # conftest 已把 repo_root 隔离为空


def test_configured_repos_composes_file_urls(monkeypatch) -> None:
    """配了 root + map → 由目录名拼出 file:// URL。"""
    from agentflow.config import get_settings
    from agentflow.workspace.prepare import configured_repos

    s = get_settings()
    monkeypatch.setattr(s, "repo_root", "/srv/repos")
    monkeypatch.setattr(s, "repo_map", '{"order-service": "aiops-test-order-service"}')
    assert configured_repos() == {
        "order-service": "file:///srv/repos/aiops-test-order-service"
    }


def test_configured_repos_keeps_explicit_scheme(monkeypatch) -> None:
    """root 已是 URL（https/file）→ 不再补 file:// 前缀。"""
    from agentflow.config import get_settings
    from agentflow.workspace.prepare import configured_repos

    s = get_settings()
    monkeypatch.setattr(s, "repo_root", "https://git.example.com/org")
    monkeypatch.setattr(s, "repo_map", '{"order-service": "order-service"}')
    assert configured_repos() == {
        "order-service": "https://git.example.com/org/order-service"
    }


def test_configured_repos_rejects_bad_json(monkeypatch) -> None:
    """repo_map 非法 JSON → 忽略覆盖而非崩溃（只打 warning）。"""
    from agentflow.config import get_settings
    from agentflow.workspace.prepare import configured_repos

    s = get_settings()
    monkeypatch.setattr(s, "repo_root", "/srv/repos")
    monkeypatch.setattr(s, "repo_map", "{not json")
    assert configured_repos() == {}


# ----------------------------------------------------------------------
# 场景 5：工具输出截断必须保留头部且显式可见
# ----------------------------------------------------------------------
def test_git_output_truncation_keeps_head_and_is_visible() -> None:
    """git diff 截断必须保留**头部**并显式标注。

    回归背景：旧实现 ``text[-4000:]`` 保留尾部，把 diff 的元信息
    （``diff --git a/... b/...`` 与 ``@@ -old,+new @@``）切掉；fix-implementer
    拿到无头 diff 后自行编造了一段假的 ``index 0000000..1111111`` 上报。
    截断若静默，模型无从知道所见内容残缺。
    """
    from agentflow.agents.workspace_tools import _truncate

    text = "diff --git a/x b/x\n@@ -1,12 +1,12 @@\n" + "x" * 500
    body, truncated = _truncate(text, 40)
    assert truncated is True
    assert body.startswith("diff --git a/x b/x")   # 头部（含路径）保留
    assert "@@" in body                            # hunk 头保留
    assert "输出被截断" in body                    # 截断显式可见
    assert "省略 5" in body or "省略" in body

    same, not_truncated = _truncate("short", 40)
    assert (same, not_truncated) == ("short", False)


# ----------------------------------------------------------------------
# 仓库自带 hook 不得在 worker 里执行（隔离：写/测试在沙箱，但 git 在 worker）
# ----------------------------------------------------------------------
async def test_ws_git_disables_repo_hooks(tmp_path: Path, monkeypatch) -> None:
    """恶意仓库的 `.git/hooks/pre-commit` 不能在 worker 容器里执行。

    仓库内容是不可信的（沙箱写进去的、来自工单定位到的代码），而 `.git/hooks/`
    也在工作区里、同样可写。`git commit` 会执行 `pre-commit`——恶意仓库因此能在
    **持有全部密钥的 worker 容器里**执行任意代码，正好绕过"写和测试进沙箱"的隔离。
    """
    from agentflow import exec_context
    from agentflow.agents.workspace_tools import ws_git
    from agentflow.config import get_settings

    root = tmp_path / "ws"
    run_id, tenant, service = "run_hook", "t1", "order-service"
    repo = root / tenant / run_id / "repos" / service
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)

    sentinel = tmp_path / "PWNED"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    hook.chmod(0o755)

    # 把 hooksPath 显式钉到 `.git/hooks`：开发机的全局 git config 可能是
    # `core.hooksPath=.githooks`，那会让 `.git/hooks/` 整体失效、hook 根本不跑——
    # 于是这条测试在**本机**永远"通过"，而**容器里**（无全局配置）漏洞是活的。
    # 钉死之后，测的是产品行为，不是这台机器的配置。
    git("config", "core.hooksPath", str(hooks), cwd=repo)

    # 自检：**不带** core.hooksPath 时这个 hook 确实会跑——否则本测试是空断言
    (repo / "a.txt").write_text("probe", encoding="utf-8")
    git("add", "a.txt", cwd=repo)
    git("commit", "-q", "-m", "probe", cwd=repo)
    assert sentinel.exists(), "自检失败：这个 hook 本来就不会执行，本测试没有意义"
    sentinel.unlink()

    monkeypatch.setattr(get_settings(), "workspace_root", root)
    tok_run = exec_context.current_run.set(run_id)
    tok_tenant = exec_context.current_tenant.set(tenant)
    try:
        (repo / "a.txt").write_text("y", encoding="utf-8")
        await ws_git(service, ["add", "a.txt"])
        # message 是 ws_git 的签名参数（只被校验、不参与构造命令，见 TODO §23.5），
        # 提交信息实际由 args 里的 -m 提供——这里按现状传，别把两件事混在一个提交里
        out = await ws_git(service, ["commit", "-m", "改一下"], message="改一下")
    finally:
        exec_context.current_run.reset(tok_run)
        exec_context.current_tenant.reset(tok_tenant)

    assert not sentinel.exists(), "仓库自带的 pre-commit 在 worker 里被执行了"
    # 正常提交不受影响（不是把 commit 一起禁掉了）
    assert out["rc"] == 0
    assert "改一下" in git("log", "-1", "--pretty=%s", cwd=repo)
