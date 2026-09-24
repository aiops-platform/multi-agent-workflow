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
        # 两种传法都收（args 里的 `-m` 与 `message=`）——这里同时给，验证不会重复加 -m
        out = await ws_git(service, ["commit", "-m", "改一下"], message="改一下")
    finally:
        exec_context.current_run.reset(tok_run)
        exec_context.current_tenant.reset(tok_tenant)

    assert not sentinel.exists(), "仓库自带的 pre-commit 在 worker 里被执行了"
    # 正常提交不受影响（不是把 commit 一起禁掉了）
    assert out["rc"] == 0
    assert "改一下" in git("log", "-1", "--pretty=%s", cwd=repo)


def test_git_argv_always_disables_hooks_and_editor() -> None:
    """两个 `-c` 是**安全项**，谁都不能拿掉；且 `-c` 必须排在子命令**之前**（git 的规矩）。

    `core.editor=true` 是 2026-09-22 加的：run_63a334c90d 的 `commit` 节点被一个
    `vi …/.git/COMMIT_EDITMSG` 永久卡住（`git commit` 没带 `-m` → git 拉起编辑器 →
    vi 继承终端 stdin）。编辑器变成 `true` 之后，这类路径只会**立刻失败**，不会挂。
    """
    from agentflow.agents.workspace_tools import _git_argv

    argv = _git_argv(["commit", "-m", "x"])
    assert argv[0] == "git"
    assert "core.hooksPath=/dev/null" in argv
    assert "core.editor=true" in argv
    assert argv.index("core.editor=true") < argv.index("commit"), "-c 是全局选项，必须在子命令前"


async def test_ws_git_commit_uses_the_message_parameter(tmp_path, monkeypatch) -> None:
    """`message=` **必须真的进命令** —— 实测 run_63a334c90d 就是这条挂死的。

    模型按**签名**填了 `message=`（`ToolSpec` 不做参数 schema，签名对 LLM 可见），而函数
    只校验、不参与构造命令（`docs/TODO.md` §23.5）→ 真跑的是裸 `git commit` → git 拉起
    编辑器 → 节点永久 running、流水一行都没有、run 行停在 `waiting_approval`。
    这里只传 `message=`、args 里**不带** `-m`，正是模型当时的传法。
    """
    import asyncio

    from agentflow.agents.workspace_tools import ws_git

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        (repo / "a.txt").write_text("y", encoding="utf-8")
        await ws_git(service, ["add", "a.txt"])
        # wait_for 兜住回归形态：万一又变成"等编辑器"，超时 → 红，而不是把测试挂死
        await asyncio.wait_for(ws_git(service, ["commit"], message="fix: 空值守卫"), timeout=20)
    finally:
        _reset(toks)

    assert git("log", "-1", "--pretty=%s", cwd=repo) == "fix: 空值守卫"


@pytest.mark.parametrize("args", [["commit"], ["commit", "-a"], ["commit", "--amend"]])
async def test_ws_git_commit_without_message_is_rejected(args, tmp_path, monkeypatch) -> None:
    """没有任何提交信息 → **报错**，而不是让 git 去开编辑器。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError, ws_git

    service, _repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError, match="需要提交信息"):
            await ws_git(service, list(args))
    finally:
        _reset(toks)


async def test_ws_git_stdin_message_fails_loudly(tmp_path, monkeypatch) -> None:
    """`-F -`（从 stdin 读提交信息）→ **响亮失败**，不挂。

    stdin 是 /dev/null，git 拿到空信息直接非零退出。与 `core.editor=true`、
    `_GIT_TIMEOUT_SEC` 一起，三条路都堵上：没有哪条能停在交互输入上。
    """
    import asyncio

    from agentflow.agents.workspace_tools import WorkspaceToolError, ws_git

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        (repo / "a.txt").write_text("y", encoding="utf-8")
        await ws_git(service, ["add", "a.txt"])
        with pytest.raises(WorkspaceToolError, match="失败"):
            await asyncio.wait_for(ws_git(service, ["commit", "-F", "-"]), timeout=20)
    finally:
        _reset(toks)


# ----------------------------------------------------------------------
# 测试命令只能来自部署配置（不接受 LLM 传参）
# ----------------------------------------------------------------------
def test_ws_run_tests_has_no_command_parameter() -> None:
    """硬保证：命令**不能传参**。

    原先签名的第二个参数是 `command`，由 LLM 传自由命令、再用前缀白名单去猜
    安不安全——而白名单里含 `"bash "`，`bash -c "<任意>"` 直接通过，等于没有白名单。
    这条测试锁住"那个参数不许回来"。
    """
    import inspect

    from agentflow.agents.workspace_tools import ws_run_tests

    params = set(inspect.signature(ws_run_tests).parameters)
    assert "command" not in params, (
        "ws_run_tests 又能传命令了——模型可以注入任意命令，白名单挡不住"
    )
    assert "service" in params


def test_test_cmd_unconfigured_is_fail_closed(monkeypatch) -> None:
    """未配置 → **报错**，不是跑一个默认命令。

    一个"跑得起来"的默认值会让配置漏了也照跑，那正是本仓反复踩的静默缺陷。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError, test_cmd_for
    from agentflow.config import get_settings

    monkeypatch.setattr(get_settings(), "test_cmds", "")
    with pytest.raises(WorkspaceToolError, match="未配置测试命令"):
        test_cmd_for("order-service")


def test_test_cmd_missing_service_lists_configured(monkeypatch) -> None:
    """配了别的服务但漏了这个 → 报错里列出已配的，便于当场发现漏配。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError, test_cmd_for
    from agentflow.config import get_settings

    monkeypatch.setattr(
        get_settings(), "test_cmds", '{"warranty-service": "./gradlew test"}'
    )
    with pytest.raises(WorkspaceToolError, match=r"已配置的服务.*warranty-service"):
        test_cmd_for("order-service")


def test_test_cmd_bad_json_is_loud(monkeypatch) -> None:
    """配置写坏了要报出来，不是静默当作"没配"。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError, test_cmd_for
    from agentflow.config import get_settings

    monkeypatch.setattr(get_settings(), "test_cmds", "{不是 JSON")
    with pytest.raises(WorkspaceToolError, match="不是合法 JSON"):
        test_cmd_for("order-service")


def test_test_cmd_returns_configured(monkeypatch) -> None:
    from agentflow.agents.workspace_tools import test_cmd_for
    from agentflow.config import get_settings

    monkeypatch.setattr(
        get_settings(), "test_cmds",
        '{"order-service": "./gradlew test --no-daemon -q"}',
    )
    assert test_cmd_for("order-service") == "./gradlew test --no-daemon -q"


# ----------------------------------------------------------------------
# 写 / 测试必须经沙箱；读刻意不经（诊断链不该被沙箱拖住）
# ----------------------------------------------------------------------
class _FakeSandbox:
    """一个**行为正确**的沙箱：它真的把文件写进工作区。

    为什么必须真写：`ws_write_file_sandboxed` 写完会在 worker 侧复读一次（判"两侧是不是
    同一个卷"），只记 `{"ok": True}` 而不落盘的假沙箱等于模拟"写到了别处"——那是**故障**形态，
    该由 `_WriteNowhere` 表达。这个替身代表的是正常环境：沙箱与 worker 挂同一个卷。
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.runs: list[tuple[str, str | None]] = []

    async def write_file(self, path: str, content: str) -> dict:
        self.writes.append((path, content))
        # 照 exec_service.write_file 的真实动作来（含建父目录），别让替身比真身"弱"：
        # 少一步就会让被测代码在别的分支上报错，测出来的东西就跑了偏。
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"written": True, "path": path}

    async def run_shell(self, cmd: str, *, cwd=None, timeout: int = 300):
        from types import SimpleNamespace

        self.runs.append((cmd, cwd))
        return SimpleNamespace(rc=0, stdout="BUILD SUCCESSFUL", stderr="", timed_out=False)


class _WriteNowhere:
    """沙箱**收下了写、回了成功**，而工作区里什么都没有 —— 两侧不是同一个卷。

    这正是 2026-09-22 的实测形态（run_fc9e158b55 的近亲）：沙箱把文件写进了容器自己的
    文件系统，宿主侧工作区一点没变，而调用方收到的是成功。工具层必须自己发现这件事。
    """

    async def write_file(self, path: str, content: str) -> dict:
        return {"written": True, "path": path, "bytes": len(content.encode("utf-8"))}

    async def run_shell(self, cmd: str, *, cwd=None, timeout: int = 300):
        from types import SimpleNamespace

        return SimpleNamespace(rc=0, stdout="", stderr="", timed_out=False)


class _UnreachableSandbox:
    async def write_file(self, path: str, content: str) -> dict:
        raise ConnectionError("connection refused")

    async def run_shell(self, cmd: str, *, cwd=None, timeout: int = 300):
        raise ConnectionError("connection refused")


def _prepared_workspace(tmp_path: Path, monkeypatch):
    """造一个已 prepare 的工作区 + 上下文，返回 (service, repo)。"""
    from agentflow import exec_context
    from agentflow.config import get_settings

    root = tmp_path / "ws"
    run_id, tenant, service = "run_sbx", "t1", "order-service"
    repo = root / tenant / run_id / "repos" / service
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)

    monkeypatch.setattr(get_settings(), "workspace_root", root)
    monkeypatch.setattr(
        get_settings(), "test_cmds", '{"order-service": "./gradlew test --no-daemon -q"}'
    )
    t1 = exec_context.current_run.set(run_id)
    t2 = exec_context.current_tenant.set(tenant)
    return service, repo, (t1, t2)


def _tool(name: str, sandbox, agent: str = "fix-implementer"):
    from agentflow.agents.tools import build_workspace_tools

    return next(
        t["func"] for t in build_workspace_tools(agent, sandbox_client=sandbox)
        if t["name"] == name
    )


def _reset(toks) -> None:
    """还原 _prepared_workspace 置位的 contextvar（避免污染同进程的其它用例）。"""
    from agentflow import exec_context

    exec_context.current_run.reset(toks[0])
    exec_context.current_tenant.reset(toks[1])


def test_write_and_tests_are_fail_closed_without_sandbox(tmp_path, monkeypatch) -> None:
    """未注入沙箱 → **调用即报错**，绝不回退本地执行。

    这是本次改造的核心保证：写文件与跑测试执行的是仓库代码，而 worker 持有全部密钥。
    "沙箱不可达就本地跑"会把隔离整个作废，且失败是静默的。
    """
    import asyncio

    from agentflow.agents.tools import build_workspace_tools
    from agentflow.agents.workspace_tools import WorkspaceToolError

    tools = {t["name"]: t["func"] for t in build_workspace_tools("fix-implementer")}
    for name in ("ws_write_file", "ws_run_tests"):
        assert name in tools, f"{name} 未注册——模型看不到它，失败会变成静默的"
        with pytest.raises(WorkspaceToolError, match="需要沙箱"):
            asyncio.run(tools[name](service="s", path="p", content="c"))
    # 读工具保持直连，不受影响
    assert "ws_read_file" in tools


async def test_write_goes_through_sandbox(tmp_path, monkeypatch) -> None:
    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    sbx = _FakeSandbox()
    try:
        out = await _tool("ws_write_file", sbx)(service=service, path="src/A.java", content="class A {}")
    finally:
        _reset(toks)

    assert sbx.writes, "写操作没有走沙箱"
    path, content = sbx.writes[0]
    assert path == str(repo / "src/A.java"), f"沙箱侧路径不一致（两侧挂同一卷）: {path}"
    assert content == "class A {}"
    assert "沙箱" in out["summary"]


async def test_sandbox_write_invisible_to_worker_is_an_error(tmp_path, monkeypatch) -> None:
    """沙箱报成功、worker 侧看不到 → **必须报错**，不许返回成功。

    实测背景（2026-09-22）：compose 只挂 `${HOME}/agentflow-workspace`，而工作区根默认是
    `/tmp/agentflow-workspace`（它在可写白名单里、却不在卷里）。沙箱于是把文件写进了
    **容器自己的文件系统**，宿主侧工作区一点没变 —— 而 `ws_write_file` 回的是
    「新建 …（沙箱）」。后果不是"报了个失败"，是 `test` 拿假失败、`commit` 空 diff：
    **整条修复链看的是改之前的代码，全程无报错**。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError, match="不是同一个工作区卷"):
            await _tool("ws_write_file", _WriteNowhere())(
                service=service, path="src/A.java", content="class A {}"
            )
    finally:
        _reset(toks)
    assert not (repo / "src/A.java").exists(), "假沙箱本就没落盘——这条测的正是'报成功但没落盘'"


async def test_tests_go_through_sandbox_with_configured_cmd(tmp_path, monkeypatch) -> None:
    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    sbx = _FakeSandbox()
    try:
        out = await _tool("ws_run_tests", sbx)(service=service)
    finally:
        _reset(toks)

    cmd, cwd = sbx.runs[0]
    assert cmd == "./gradlew test --no-daemon -q"  # 来自配置，不是调用方传的
    assert cwd == str(repo)
    assert out["passed"] is True


async def test_unreachable_sandbox_does_not_fall_back(tmp_path, monkeypatch) -> None:
    """沙箱连不上 → 报错，**不是**悄悄在 worker 本地跑。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError, match="不会.*回退"):
            await _tool("ws_write_file", _UnreachableSandbox())(
                service=service, path="a.txt", content="x"
            )
    finally:
        _reset(toks)

    # 断言真的没写进去（不是"报了错但文件已经落了"）
    assert (repo / "a.txt").read_text(encoding="utf-8") == "x"



# ----------------------------------------------------------------------
# ws_open_pr：推分支 + 开 PR（治「pr_url 系统性为空」）
# ----------------------------------------------------------------------
class _FakeProc:
    """gh 调用替身：只为 `_run` 提供 communicate()/returncode。

    **必须能返回 stderr** —— `_run` 现在是 `out, err = communicate()`。
    早期这里返回 `(out, None)`，于是 `_run` 拆包就炸；而更值得注意的是
    下面 `_stub_gh` 默认**往 stderr 塞一句话**：真实 `gh` 就会这么干，
    而早期实现把 stderr 并进了 stdout，JSON 解析于是崩在一句英文上
    （实测 run_d595720e5b 的 commit 节点连试三次都是这个）。
    """

    def __init__(self, out: str, rc: int = 0, err: str = "") -> None:
        self._out = out.encode()
        self._err = err.encode()
        self.returncode = rc

    async def communicate(self):
        return self._out, self._err


def _stub_gh(monkeypatch, *, existing: str = "", base: str = "main",
             create_url: str = "https://github.com/o/r/pull/12",
    gh_stderr: str = "warning: some gh notice on stderr\n"):
    """拦下 `gh` 调用并记录 argv；**其余（git push 等）走真的**。

    只桩 gh 是有意的：push 是真的在跑，所以"分支到底推出去没有"由裸仓库自己作证，
    而不是由我们的桩点头。

    ``gh_stderr`` **默认非空** —— 真实 `gh` 会往 stderr 写提示。把它默认留空的话，
    测试就复现不了「stderr 混进 stdout 导致 JSON 解析崩」那个 bug（已实测踩过）。
    """
    import asyncio as _a

    calls: list[list[str]] = []
    real = _a.create_subprocess_exec

    async def fake(*argv, **kw):
        # ⚠️ `*argv` 收的是 **tuple**：`argv[1:3] == ["repo", "view"]` 恒为 False
        #（tuple 永不等于 list）。第一版就是这么挂的，且症状是"桩没生效、走了兜底"。
        a = list(argv)
        if a and a[0] == "gh":
            calls.append(a)
            if a[1:2] == ["pr"] and "list" in a:
                return _FakeProc(existing, err=gh_stderr)
            if a[1:3] == ["repo", "view"]:
                return _FakeProc(base + "\n", err=gh_stderr)
            if a[1:2] == ["pr"] and "create" in a:
                return _FakeProc(create_url + "\n", err=gh_stderr)
            return _FakeProc("", 1, err=gh_stderr)
        return await real(*argv, **kw)

    monkeypatch.setattr(_a, "create_subprocess_exec", fake)
    return calls


def _with_origin(repo: Path, tmp_path: Path) -> Path:
    """挂一个 origin：**fetch URL 长得像 GitHub，push 真的落在本地裸仓库**。

    两个 URL 分工不是花招，是必须的：
    - `get-url origin`（我的检查读它）必须是 `https://github.com/...`，
      否则过不了"origin 得是 GitHub 远端"那道闸；
    - push 打真实 GitHub 是不可能的（无网/无凭证），所以 `--push` 指向裸仓库 ——
      这样"分支到底推出去没有"仍由裸仓库自己作证，而不是由桩点头。
    """
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(origin))
    git("remote", "add", "origin",
        "https://github.com/xqfgbc/aiops-test-order-service.git", cwd=repo)
    git("remote", "set-url", "--push", "origin", str(origin), cwd=repo)
    return origin


async def test_ws_open_pr_pushes_branch_and_returns_real_pr(tmp_path, monkeypatch) -> None:
    """推分支 + 开 PR：**分支真的到远端**，pr_url 来自 gh 的返回。

    实测背景（run_170dccffd9）：committer 的提示词只有 add/commit/rev-parse 三步，
    输出契约里却声明了 pr_url —— 那个字段于是**系统性地永远是空串**，
    每一次「修复成功」都被 ticket-done 回传成 failed。
    """
    from agentflow.agents.workspace_tools import ws_open_pr

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    origin = _with_origin(repo, tmp_path)
    git("checkout", "-q", "-b", "aiops/RUN_x", cwd=repo)
    (repo / "fix.txt").write_text("fixed", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "fix", cwd=repo)
    calls = _stub_gh(monkeypatch)
    try:
        out = await ws_open_pr(service=service, title="--加固磁盘写满", body="根因：ENOSPC")
    finally:
        _reset(toks)

    # ① 分支真的推上去了（裸仓库作证，不是桩说了算）
    assert git("rev-parse", "--verify", "aiops/RUN_x", cwd=origin)
    # ② 返回的是 gh 给的 PR，不是自己编的
    assert out["pr_url"] == "https://github.com/o/r/pull/12"
    assert out["pr_number"] == 12 and out["created"] is True

    create = next(c for c in calls if c[1:2] == ["pr"] and "create" in c)
    # ③ base 取仓库默认分支，不由调用方传
    assert "--base" in create and create[create.index("--base") + 1] == "main"
    # ④ 标题走 `--title=` 单参形式 —— 以 `-` 开头的标题不会被 gh 当旗标解析
    assert "--title=--加固磁盘写满" in create
    assert not any(a == "--title" for a in create)


async def test_ws_open_pr_reuses_existing_pr(tmp_path, monkeypatch) -> None:
    """同 head 已开着 PR 就复用 —— 节点内重试不该把一次成功的提交变成失败。

    （节点级幂等键 run_id:node_id 挡住的是**跨节点重放**；节点**内**的重试
    仍会再次调到这里，那时 gh 会因为"PR 已存在"而报错。）
    """
    from agentflow.agents.workspace_tools import ws_open_pr

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    calls = _stub_gh(monkeypatch, existing='{"url":"https://github.com/o/r/pull/7","number":7}')
    try:
        out = await ws_open_pr(service=service, title="x")
    finally:
        _reset(toks)

    assert out["pr_number"] == 7 and out["created"] is False
    assert not any("create" in c for c in calls), "已有 PR 时不该再 create（会报 already exists）"


async def test_ws_open_pr_refuses_empty_title(tmp_path, monkeypatch) -> None:
    """空标题直接拒 —— 不要拿一个空 PR 出去。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError, ws_open_pr

    service, _repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError):
            await ws_open_pr(service=service, title="   ")
    finally:
        _reset(toks)


async def test_ws_open_pr_refuses_non_github_origin(tmp_path, monkeypatch) -> None:
    """origin 不是 GitHub 远端 → **明确报错**，不要推完再让 gh 说一句看不懂的。

    本地联调形态（`AGENTFLOW_REPO_ROOT` 指向本机 testbed 副本）就是这样：
    工作区从本机路径克隆，origin 是 `file:///Users/...`。此时：
      ① push 推得动，但推到的是**本机那份副本** —— 在一个不相干的地方留下分支；
      ② `gh` 只会往 stderr 说「none of the git remotes ... point to a known GitHub host」，
         而那句英文混进 stdout 后表现为 `json.loads: Expecting value` ——
         **现场完全看不出是远端的问题**（实测 run_d595720e5b）。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError, ws_open_pr

    # 刻意**不挂 origin**：`_prepared_workspace` 造出来的工作区没有远端，
    # 正是"origin 不是 GitHub 远端"这一档
    service, _repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError) as ei:
            await ws_open_pr(service=service, title="x")
    finally:
        _reset(toks)
    assert "不是 GitHub 远端" in str(ei.value)
    assert "AGENTFLOW_REPO_ROOT" in str(ei.value), "错误里要给出可操作的下一步"


async def test_ws_open_pr_ignores_gh_stderr(tmp_path, monkeypatch) -> None:
    """`gh` 往 stderr 写东西**不影响** stdout 的 JSON 解析。

    真实 `gh` 会输出提示（认证提醒、远端识别提示……）。早期 `_run` 用
    `stderr=STDOUT` 把两者并起来，于是 `json.loads` 撞在一句英文上 ——
    错误是 `Expecting value: line 1 column 1`，**指不到真正的原因**。
    """
    from agentflow.agents.workspace_tools import ws_open_pr

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    calls = _stub_gh(monkeypatch, existing='{"url":"https://github.com/o/r/pull/9","number":9}',
                     gh_stderr="warning: 认证快要过期了\n又一行噪音\n")
    try:
        out = await ws_open_pr(service=service, title="x")
    finally:
        _reset(toks)
    assert out["pr_number"] == 9, "stderr 不该污染 stdout 的 JSON"
    assert any("list" in c for c in calls)


# ----------------------------------------------------------------------
# `_run`（ws_open_pr 用的执行器）同样不许停在交互输入上
# ----------------------------------------------------------------------
# 为什么单独测这个私有函数：它跑的是 `git push` 与 `gh`，而"缺凭证时 git 会在终端上
# 问用户名"这条路径**没法用真 git 复现**（要真有个需要认证的远端）。这里测的是
# 它赖以不挂的三个开关本身，值不值得单测见 run_63a334c90d 那次 14 分钟的挂死。
async def test_run_gives_subprocess_no_terminal() -> None:
    """stdin 是 /dev/null：要读输入的命令**立刻失败**，而不是等着人敲键盘。

    `read x` 在 EOF 上返回非零 —— 换成真终端的话它会一直等，正是我们要防的那件事。

    ⚠️ **这条抓不住"忘了传 DEVNULL"**：pytest 自己的 stdin 本来就不是 tty，去掉
    `stdin=DEVNULL` 这里照样会 EOF。它锁的是"子进程拿不到任何输入"这个**契约**，
    tty 场景靠的是那三个环境开关（下一条用例，那条能抓住变异）。
    """
    import asyncio

    from agentflow.agents.workspace_tools import WorkspaceToolError, _run

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(WorkspaceToolError, match="失败"):
        await _run(Path("/tmp"), ["sh", "-c", "read x"])
    assert loop.time() - started < 5, "应该立刻失败，而不是等输入"


async def test_run_disables_all_interactive_prompts() -> None:
    """两个"别问、直接失败"的开关真的到了子进程环境里（git 凭证 / gh 提示）。"""
    from agentflow.agents.workspace_tools import _run

    out = await _run(Path("/tmp"), ["sh", "-c",
                                    "echo $GIT_TERMINAL_PROMPT/$GH_PROMPT_DISABLED/$GIT_PAGER"])
    assert out.strip() == "0/1/cat", out


async def test_run_times_out_instead_of_hanging(monkeypatch) -> None:
    """超时 → kill + 抛错。没有它，"停住"就是永久（节点永远 running、run 卡死）。"""
    import asyncio

    from agentflow.agents import workspace_tools
    from agentflow.agents.workspace_tools import WorkspaceToolError, _run

    monkeypatch.setattr(workspace_tools, "_SUBPROC_TIMEOUT_SEC", 0.3)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(WorkspaceToolError, match="超时"):
        await _run(Path("/tmp"), ["sh", "-c", "sleep 30"])
    assert loop.time() - started < 5, "应该在超时点附近就返回，而不是等命令自己结束"


# ----------------------------------------------------------------------
# ws_merge_pr（`agents/release_tools.py`）—— 本仓第一个**不可逆**动作
# ----------------------------------------------------------------------
# 用例放在这个文件而不是新开一个：`_FakeProc` / `_with_origin` / `_prepared_workspace`
# 这些夹具都在这里，复制一份出去就是又一次"两份实现必然漂移"（CLAUDE.md §11）。
_GH_OWNER_REPO = ("xqfgbc", "aiops-test-order-service")
_PR_URL = f"https://github.com/{_GH_OWNER_REPO[0]}/{_GH_OWNER_REPO[1]}/pull/7"


def _stub_gh_merge(monkeypatch, *, state="OPEN", head_ref="main", head_oid="",
                   merge_state="CLEAN", merge_sha="d34db33f0000",
                   merge_response=None, gh_stderr=""):
    """桩掉 `gh pr view` 与 `gh api .../merge`，返回记录下来的 argv 列表。

    **只桩 gh**：`git remote get-url origin` 与 `git rev-parse` 走真的 ——
    于是"这个 PR 的头到底是不是工作区这个 commit"由**真实仓库**作证，
    而不是由桩点头（与 `_stub_gh` 同一条原则）。
    """
    import asyncio as _a
    import json as _json

    calls: list[list[str]] = []
    real = _a.create_subprocess_exec

    async def fake(*argv, **kw):
        # ⚠️ `*argv` 收的是 tuple，切片的比较要用 list（第一版栽在这上面，见 `_stub_gh`）
        a = list(argv)
        if a and a[0] == "gh":
            calls.append(a)
            if a[1:2] == ["pr"] and "view" in a:
                return _FakeProc(_json.dumps({
                    "state": state, "headRefName": head_ref, "headRefOid": head_oid,
                    "mergeCommit": ({"oid": merge_sha} if state == "MERGED" else None),
                    "mergeStateStatus": merge_state,
                }), err=gh_stderr)
            if a[1:2] == ["api"]:
                body = merge_response if merge_response is not None else {
                    "merged": True, "sha": merge_sha, "message": "Pull Request successfully merged",
                }
                return _FakeProc(_json.dumps(body), err=gh_stderr)
            return _FakeProc("", 1, err=gh_stderr)
        return await real(*argv, **kw)

    monkeypatch.setattr(_a, "create_subprocess_exec", fake)
    return calls


def _merge_calls(calls) -> list[list[str]]:
    return [c for c in calls if c[1:2] == ["api"]]


async def test_ws_merge_pr_merges_and_returns_the_merge_commit(tmp_path, monkeypatch) -> None:
    """正常路径：squash 合并，`merge_commit` 取自 gh 的返回（那是**主干上**那个提交）。"""
    from agentflow.agents.release_tools import ws_merge_pr

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo)
    calls = _stub_gh_merge(monkeypatch, head_oid=head)
    try:
        out = await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)

    assert out["merged"] is True
    assert out["already_merged"] is False
    assert out["merge_commit"] == "d34db33f0000"
    assert out["pr_number"] == 7
    puts = _merge_calls(calls)
    assert len(puts) == 1, f"应当只调一次合并，实际 {len(puts)}"
    assert "merge_method=squash" in puts[0], "合并方式固定 squash（不让模型挑历史形状）"


async def test_ws_merge_pr_rejects_a_non_github_pr_url(tmp_path, monkeypatch) -> None:
    """`pr_url` 不是 GitHub PR 链接 → 立刻拒，且**一次 gh 都不调**（本地就判掉了）。"""
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    calls = _stub_gh_merge(monkeypatch)
    try:
        for bad in ("", "7", "https://gitlab.com/o/r/pull/7",
                    "https://github.com/o/r/issues/7"):
            with pytest.raises(WorkspaceToolError, match="不是 GitHub PR 链接"):
                await ws_merge_pr(service, bad)
    finally:
        _reset(toks)
    assert calls == [], "本地就能判掉的不该去问 gh"


async def test_ws_merge_pr_rejects_a_pr_from_another_repo(tmp_path, monkeypatch) -> None:
    """commit 说 PR 在 A，工作区却在 B → 拒，且**不调合并**。

    这条挡的是"合了别人的分支"：本仓 main 上堆着多个历史 run 留下的未合并 PR，
    一个错/编的链接在过去会被 `gh` 照单全收。
    """
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)  # origin = xqfgbc/aiops-test-order-service
    calls = _stub_gh_merge(monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError, match="拒绝合并"):
            await ws_merge_pr(service, "https://github.com/someone/other-repo/pull/7")
    finally:
        _reset(toks)
    assert _merge_calls(calls) == []


@pytest.mark.parametrize("state", ["CLOSED", "DRAFT"])
async def test_ws_merge_pr_rejects_a_pr_that_is_not_open(state, tmp_path, monkeypatch) -> None:
    """只有 OPEN 能合：被关掉的 / 还是草稿的都要拦下来。"""
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    calls = _stub_gh_merge(monkeypatch, state=state)
    try:
        with pytest.raises(WorkspaceToolError, match=state):
            await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)
    assert _merge_calls(calls) == []


async def test_ws_merge_pr_refuses_when_the_pr_head_is_not_our_commit(
    tmp_path, monkeypatch
) -> None:
    """PR 的头 ≠ 工作区 HEAD → 拒。

    这是「**声称改了 ≠ 真改了**」（§3.3）在合并上的对应物：`commit` 说"我开了 PR"，
    这里去远端核一句"那个 PR 的头到底是不是我这个 commit"，而不是信它。
    """
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    calls = _stub_gh_merge(monkeypatch, head_oid="0" * 40)  # 不是工作区的 HEAD
    try:
        with pytest.raises(WorkspaceToolError, match="不是同一份代码"):
            await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)
    assert _merge_calls(calls) == []


async def test_ws_merge_pr_refuses_when_the_pr_is_not_from_our_branch(
    tmp_path, monkeypatch
) -> None:
    """PR 的源分支 ≠ 工作区当前分支 → 拒。"""
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo)
    calls = _stub_gh_merge(monkeypatch, head_ref="aiops/RUN_somebody_else", head_oid=head)
    try:
        with pytest.raises(WorkspaceToolError, match="不是本次 run 的分支"):
            await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)
    assert _merge_calls(calls) == []


async def test_ws_merge_pr_refuses_a_conflicting_pr(tmp_path, monkeypatch) -> None:
    """与主干冲突 → 提前给一句人能看懂的话，而不是把 gh 的 405 原样抛出来。

    本仓必然撞到这条：main 上堆着多个同文件的未合并 PR，谁先合谁让其余的全变冲突。

    ⚠️ 冲突态的取值是 **`DIRTY`**。第一版写的是 `CONFLICTING` —— 那是 **GraphQL
    `mergeable`** 的取值，`mergeStateStatus` 里根本没有它，于是这条守卫**永远不触发**，
    而测试也照样绿（桩按我写错的值回话，自己验自己）。是拿真实 PR 跑了一遍才看出来的：
    本仓 PR #11 实测就是 `DIRTY`。
    """
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo)
    calls = _stub_gh_merge(monkeypatch, head_oid=head, merge_state="DIRTY")
    try:
        with pytest.raises(WorkspaceToolError, match="rebase"):
            await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)
    assert _merge_calls(calls) == []


async def test_ws_merge_pr_is_idempotent_when_already_merged(tmp_path, monkeypatch) -> None:
    """**已经合过就复用既有结果，绝不第二次合并。**

    这条是承重的，不是锦上添花：`execute_with_idempotency` 的缓存只在 agent
    **吐出合法 JSON** 时才记成功。若 agent 已经合成功了、却在收尾时失败
    （`AgentOutputError`，本仓高频），那条 attempt 记的是 failed ⇒ resume 时节点重跑
    ⇒ **本工具被真真切切再调一次**。没有这个回落就会报"PR 找不到" → `on_failure: abort`
    → **把已经发生的合并报成失败**。

    判据：重跑一次，外部世界不该再多一次可见的变化。
    """
    from agentflow.agents.release_tools import ws_merge_pr

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    calls = _stub_gh_merge(monkeypatch, state="MERGED", head_oid="0" * 40,
                           merge_sha="abcabcabcabc")
    try:
        out = await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)

    assert out["merged"] is True
    assert out["already_merged"] is True, "要如实说这是复用，不是本次合的"
    assert out["merge_commit"] == "abcabcabcabc"
    # ★ 这条是整个用例的重点：**PUT 一次都不能有**
    assert _merge_calls(calls) == [], "已经合并过的 PR 又被合了一次"


async def test_ws_merge_pr_is_loud_without_gh(tmp_path, monkeypatch) -> None:
    """本机没有 gh → 报一句能操作的错，别让它退化成 FileNotFoundError。"""
    from agentflow.agents import release_tools
    from agentflow.agents.release_tools import ws_merge_pr
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    monkeypatch.setattr(release_tools.shutil, "which", lambda _name: None)
    try:
        with pytest.raises(WorkspaceToolError, match="gh"):
            await ws_merge_pr(service, _PR_URL)
    finally:
        _reset(toks)


async def test_github_origin_guard_rejects_non_github_hosts(tmp_path, monkeypatch) -> None:
    """origin 长得像远端但**不是 GitHub** 的也要拦（GitLab 一类）。

    判据必须是"解析出来是不是 github.com"，不能只看有没有 `scheme://` ——
    `AGENTFLOW_REPO_ROOT` 配成别的 org URL 时，origin 看上去完全正常，
    而 `gh` 一样用不了，且比 `file://` 更难看出来。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError, _require_github_origin

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        git("remote", "add", "origin", "https://gitlab.com/o/r.git", cwd=repo)
        with pytest.raises(WorkspaceToolError, match="不是 GitHub 远端"):
            await _require_github_origin(repo)
        # 换回 GitHub 形态就放行（同一个函数，正反两面都锁）
        git("remote", "set-url", "origin", "https://github.com/o/r.git", cwd=repo)
        assert await _require_github_origin(repo) == "https://github.com/o/r.git"
    finally:
        _reset(toks)


# ----------------------------------------------------------------------
# ci 节点：ws_build_artifact（沙箱编译打包）+ ws_build_image（宿主构建镜像）
# ----------------------------------------------------------------------
# 判据与 `ws_run_tests` 那一族同源，但多两层：**比树**（要构建的 == 主干上的）与
# **复读**（产物在 worker 侧真的看得见）。两条都不是洁癖，见各自的 docstring。
_BUILD_CMDS = '{"order-service": "./gradlew clean bootJar --no-daemon -q"}'
_TRUNK_SHA = "211eae4e251878d52498a6eb0072dc7d3d74e474"


def _stub_gh_tree(monkeypatch, tree: str):
    """桩掉 `gh api .../commits/<sha> --jq .commit.tree.sha`；其余走真的。

    只桩这一条查询是有意的：`git rev-parse HEAD^{tree}` 走**真实仓库**，
    于是"树相不相等"由仓库自己作证，而不是由桩点头（同 `_stub_gh` 的原则）。
    """
    import asyncio as _a

    calls: list[list[str]] = []
    real = _a.create_subprocess_exec

    async def fake(*argv, **kw):
        a = list(argv)
        if a and a[0] == "gh":
            calls.append(a)
            return _FakeProc(tree + "\n")
        return await real(*argv, **kw)

    monkeypatch.setattr(_a, "create_subprocess_exec", fake)
    return calls


class _BuildOKSandbox(_FakeSandbox):
    """构建成功的沙箱：**真的在 cwd 下落一个 jar**（两侧同一个卷）。

    照 `_FakeSandbox` 的规矩：替身不能比真身"弱"。只回 rc=0 而不落产物，
    模拟的是"沙箱写到了别处"——那是**故障**形态，该由 `_BuildNowhere` 表达。
    """

    async def run_shell(self, cmd: str, *, cwd=None, timeout: int = 300):
        from types import SimpleNamespace

        self.runs.append((cmd, cwd))
        libs = Path(cwd) / "build" / "libs"
        libs.mkdir(parents=True, exist_ok=True)
        (libs / "order-service-0.0.1-SNAPSHOT.jar").write_bytes(b"jar" * 100)
        return SimpleNamespace(rc=0, stdout="BUILD SUCCESSFUL", stderr="", timed_out=False)


class _BuildNowhere(_FakeSandbox):
    """沙箱回 rc=0、**但工作区里没有产物** —— 两侧不是同一个卷（§9.6 那个疤）。"""

    async def run_shell(self, cmd: str, *, cwd=None, timeout: int = 300):
        from types import SimpleNamespace

        self.runs.append((cmd, cwd))
        return SimpleNamespace(rc=0, stdout="BUILD SUCCESSFUL", stderr="", timed_out=False)


def test_build_cmd_unconfigured_is_fail_closed(monkeypatch) -> None:
    """未配置打包命令 → **报错**，不给默认值代跑（同 `test_cmd_for` 的判据）。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError, build_cmd_for
    from agentflow.config import get_settings

    monkeypatch.setattr(get_settings(), "build_cmds", "")
    with pytest.raises(WorkspaceToolError, match="AGENTFLOW_BUILD_CMDS"):
        build_cmd_for("order-service")


def test_build_cmd_missing_service_lists_configured(monkeypatch) -> None:
    """服务不在配置里 → 报错里**列出已配置的服务**（别让人去猜拼写）。"""
    from agentflow.agents.workspace_tools import WorkspaceToolError, build_cmd_for
    from agentflow.config import get_settings

    monkeypatch.setattr(get_settings(), "build_cmds", _BUILD_CMDS)
    with pytest.raises(WorkspaceToolError, match="order-service"):
        build_cmd_for("warranty-service")


def test_image_tag_uses_the_trunk_commit_not_the_workspace_head(tmp_path, monkeypatch) -> None:
    """tag = `<service>:<主干 merge_commit 前 12 位>` —— **不是**工作区 HEAD 的 sha。

    这条是 D4 的判据：squash 之后分支头在主干上是孤儿（实测 `run_e12a47ed4d`：
    工作区 HEAD `137a75df…`、主干 `211eae4e…`）。拿工作区的 sha 当 tag，
    等于给镜像挂一个**主干上查无此人**的名字。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError, image_tag_for

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    try:
        head = git("rev-parse", "HEAD", cwd=repo)
        tag = image_tag_for(service, _TRUNK_SHA)
        assert tag == f"order-service:{_TRUNK_SHA[:12]}"
        assert head[:12] != _TRUNK_SHA[:12], "本用例的前提：两个 sha 本来就不一样"
        assert head[:12] not in tag, "tag 里绝不能出现工作区 HEAD 的 sha"
        with pytest.raises(WorkspaceToolError, match="merge_commit 为空"):
            image_tag_for(service, "")
    finally:
        _reset(toks)


async def test_ws_build_artifact_builds_through_the_sandbox(tmp_path, monkeypatch) -> None:
    """正常路径：比树通过 → 沙箱里跑配置里那条命令 → 产物在 worker 侧复读得到。"""
    from agentflow.agents.workspace_tools import ws_build_artifact_sandboxed
    from agentflow.config import get_settings

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    tree = git("rev-parse", "HEAD^{tree}", cwd=repo)
    _stub_gh_tree(monkeypatch, tree)
    monkeypatch.setattr(get_settings(), "build_cmds", _BUILD_CMDS)
    sandbox = _BuildOKSandbox()
    try:
        out = await ws_build_artifact_sandboxed(sandbox, service, _TRUNK_SHA)
    finally:
        _reset(toks)

    assert out["built"] is True
    assert out["artifact"] == "build/libs/order-service-0.0.1-SNAPSHOT.jar"
    assert out["artifact_bytes"] > 0
    assert len(sandbox.runs) == 1
    cmd, cwd = sandbox.runs[0]
    assert cmd == "./gradlew clean bootJar --no-daemon -q"   # 命令来自配置，不是调用方
    assert Path(cwd) == repo                                  # 就跑在那个仓里


async def test_ws_build_artifact_refuses_when_the_tree_differs(tmp_path, monkeypatch) -> None:
    """树不匹配 → 在**比树那一步**就拒，沙箱一次都不调。

    挡的是"构建了别的东西"：工作区被人动过、或 merge_commit 不是这棵树对应的提交。
    先拦能省下一次 8.5 秒的构建，更重要的是**不产生一个来源不明的产物**。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError, ws_build_artifact_sandboxed
    from agentflow.config import get_settings

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    _stub_gh_tree(monkeypatch, "0" * 40)          # 主干上的树 ≠ 工作区的树
    monkeypatch.setattr(get_settings(), "build_cmds", _BUILD_CMDS)
    sandbox = _BuildOKSandbox()
    try:
        with pytest.raises(WorkspaceToolError, match="不是同一份代码"):
            await ws_build_artifact_sandboxed(sandbox, service, _TRUNK_SHA)
    finally:
        _reset(toks)
    assert sandbox.runs == [], "树都不匹配就不该去构建"


async def test_ws_build_artifact_is_invisible_to_worker_is_an_error(
    tmp_path, monkeypatch
) -> None:
    """沙箱回 rc=0，但 worker 侧**看不到产物** → 报错，绝不报成功。

    §9.6 那个疤的同一条判据：两侧挂的不是同一个卷时，沙箱会把东西写进容器自己的
    文件系统、回一句成功，而宿主侧什么都没变。构建产物是整条链里最不该静默丢的东西。
    """
    from agentflow.agents.workspace_tools import WorkspaceToolError, ws_build_artifact_sandboxed
    from agentflow.config import get_settings

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    _stub_gh_tree(monkeypatch, git("rev-parse", "HEAD^{tree}", cwd=repo))
    monkeypatch.setattr(get_settings(), "build_cmds", _BUILD_CMDS)
    try:
        with pytest.raises(WorkspaceToolError, match="看不到产物"):
            await ws_build_artifact_sandboxed(_BuildNowhere(), service, _TRUNK_SHA)
    finally:
        _reset(toks)


async def test_ws_build_artifact_failure_keeps_the_log_tail(tmp_path, monkeypatch) -> None:
    """构建失败：`built=False`，且 `log_tail` 保**尾部**（gradle 的结论在末尾）。"""
    from types import SimpleNamespace

    from agentflow.agents.workspace_tools import ws_build_artifact_sandboxed
    from agentflow.config import get_settings

    class _Failing(_FakeSandbox):
        async def run_shell(self, cmd, *, cwd=None, timeout=300):
            self.runs.append((cmd, cwd))
            return SimpleNamespace(rc=1, stdout="x" * 20000 + "\nFAILURE: Build failed",
                                   stderr="", timed_out=False)

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    _with_origin(repo, tmp_path)
    _stub_gh_tree(monkeypatch, git("rev-parse", "HEAD^{tree}", cwd=repo))
    monkeypatch.setattr(get_settings(), "build_cmds", _BUILD_CMDS)
    try:
        out = await ws_build_artifact_sandboxed(_Failing(), service, _TRUNK_SHA)
    finally:
        _reset(toks)

    assert out["built"] is False
    assert out["rc"] == 1
    assert out["log_tail"].endswith("FAILURE: Build failed"), "截断必须留尾部"
    assert "已省略" in out["log_tail"], "截断处要显式标注，模型才知道看到的是残缺内容"


def test_ws_build_artifact_is_fail_closed_without_sandbox() -> None:
    """未接线沙箱 → **调用即报错**，绝不回退本地执行（编译跑的是仓库代码）。"""
    import asyncio

    from agentflow.agents.tools import build_workspace_tools
    from agentflow.agents.workspace_tools import WorkspaceToolError

    tools = {t["name"]: t["func"] for t in build_workspace_tools("ci-builder")}
    assert "ws_build_artifact" in tools, "模型看不到它，失败会变成静默的"
    with pytest.raises(WorkspaceToolError, match="需要沙箱"):
        asyncio.run(tools["ws_build_artifact"](service="s", merge_commit="abc"))


def _stub_docker(monkeypatch, *, inspect_out: str = "sha256:deadbeef 35285684"):
    """桩 `docker build` / `docker image inspect`，记录 argv；其余走真的。"""
    import asyncio as _a

    calls: list[list[str]] = []
    real = _a.create_subprocess_exec

    async def fake(*argv, **kw):
        a = list(argv)
        if a and a[0] == "docker":
            calls.append(a)
            if a[1:2] == ["image"]:
                return _FakeProc(inspect_out + "\n")
            return _FakeProc("")
        return await real(*argv, **kw)

    monkeypatch.setattr(_a, "create_subprocess_exec", fake)
    return calls


async def test_ws_build_image_uses_the_trunk_tag_and_verifies_it(
    tmp_path, monkeypatch
) -> None:
    """正常路径：上下文就是那个仓、tag 由 merge_commit 推出，且**回读镜像真的在**。"""
    from agentflow.agents import release_tools
    from agentflow.agents.release_tools import ws_build_image

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    # 产物先就位（上一步 ws_build_artifact 的产出）
    libs = repo / "build" / "libs"
    libs.mkdir(parents=True, exist_ok=True)
    (libs / "order-service-0.0.1-SNAPSHOT.jar").write_bytes(b"jar" * 100)
    monkeypatch.setattr(release_tools.shutil, "which", lambda _n: "/usr/bin/docker")
    calls = _stub_docker(monkeypatch)
    try:
        out = await ws_build_image(service, _TRUNK_SHA)
    finally:
        _reset(toks)

    assert out["image_built"] is True
    assert out["image_tag"] == f"order-service:{_TRUNK_SHA[:12]}"
    assert out["image_id"] == "sha256:deadbeef"
    assert out["image_bytes"] == 35285684
    builds = [c for c in calls if c[1:2] == ["build"]]
    assert len(builds) == 1
    assert builds[0][-1] == ".", "构建上下文是当前仓（cwd 已经是那个仓）"
    assert out["image_tag"] in builds[0], "tag 必须出现在 docker build 的 argv 里"
    # 回读：只回 build 的 rc 不证明镜像真的进了本地 store
    assert any(c[1:3] == ["image", "inspect"] for c in calls)


async def test_ws_build_image_refuses_without_an_artifact(tmp_path, monkeypatch) -> None:
    """产物不在 → 拒，**docker 一次都不调**。

    不先查的话，`docker build` 会以 `COPY failed: no source files` 报出来 ——
    那句话看不出是"上一步没跑"。
    """
    from agentflow.agents import release_tools
    from agentflow.agents.release_tools import ws_build_image
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, _repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(release_tools.shutil, "which", lambda _n: "/usr/bin/docker")
    calls = _stub_docker(monkeypatch)
    try:
        with pytest.raises(WorkspaceToolError, match="先跑 ws_build_artifact"):
            await ws_build_image(service, _TRUNK_SHA)
    finally:
        _reset(toks)
    assert calls == []


async def test_ws_build_image_is_loud_when_the_image_is_missing(
    tmp_path, monkeypatch
) -> None:
    """`docker build` 说成功、但 `docker image inspect` 查不到 → 报错。"""
    from agentflow.agents import release_tools
    from agentflow.agents.release_tools import ws_build_image
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    libs = repo / "build" / "libs"
    libs.mkdir(parents=True, exist_ok=True)
    (libs / "order-service-0.0.1-SNAPSHOT.jar").write_bytes(b"jar" * 100)
    monkeypatch.setattr(release_tools.shutil, "which", lambda _n: "/usr/bin/docker")
    _stub_docker(monkeypatch, inspect_out="")
    try:
        with pytest.raises(WorkspaceToolError, match="查不到这个镜像"):
            await ws_build_image(service, _TRUNK_SHA)
    finally:
        _reset(toks)


async def test_ws_build_image_is_loud_without_docker(tmp_path, monkeypatch) -> None:
    """本机没有 docker → 报一句能操作的错（发布链只在本地进程形态可用）。"""
    from agentflow.agents import release_tools
    from agentflow.agents.release_tools import ws_build_image
    from agentflow.agents.workspace_tools import WorkspaceToolError

    service, _repo, toks = _prepared_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(release_tools.shutil, "which", lambda _n: None)
    try:
        with pytest.raises(WorkspaceToolError, match="docker"):
            await ws_build_image(service, _TRUNK_SHA)
    finally:
        _reset(toks)


def test_parse_github_remote_accepts_both_url_styles() -> None:
    """https 与 `git@host:` 两种写法都要认；非 GitHub 一律 None。"""
    from agentflow.agents.workspace_tools import _parse_github_remote

    assert _parse_github_remote("https://github.com/o/r.git") == ("o", "r")
    assert _parse_github_remote("https://github.com/o/r") == ("o", "r")
    assert _parse_github_remote("https://github.com/o/r/") == ("o", "r")
    assert _parse_github_remote("git@github.com:o/r.git") == ("o", "r")
    assert _parse_github_remote("ssh://git@github.com/o/r.git") == ("o", "r")
    for bad in ("file:///Users/x/repos/r", "https://gitlab.com/o/r", "/abs/path", "", "o/r"):
        assert _parse_github_remote(bad) is None, bad
