"""工作区工具（修复侧 L2）：让 fix-implementer / tester / reviewer / committer 在
**本次 run 自己的工作区**里真实读写代码与执行 git，替代 M7 脚本里的硬编码字符串替换。

工作区定位（§8.7.2 布局 ``{root}/{tenant}/{run}/repos/{service}``）：
- run/tenant 一律取自执行上下文（``exec_context.current_run`` / ``current_tenant``，
  由 DAGExecutor 在节点执行前置位），**不接受 LLM 传参**——防 agent 越界到别的 run；
- service 由 agent 传参，但只允许解析到本次 run 已 prepare 的仓库目录
  （``repos/<service>`` 必须存在且含 .git），否则拒绝。

写入安全（§10.3 ActionExecutor 同款白名单思路）：
- 路径必须落在工作区目录内（realpath 前缀校验，堵 ``../`` 逃逸）；
- 拒绝写入 ``.git/`` 内部；
- 单次写入内容上限 ``_MAX_WRITE_BYTES``。

git 命令白名单：只放行 status/diff/add/commit/push/rev-parse/branch/checkout。
``git pull``/``fetch``/``reset`` 一律不提供——run 期间版本冻结（§4.6 无 git_pull）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

from ..exec_context import current_run, current_tenant
from ..workspace.manager import WorkspaceManager

_MAX_WRITE_BYTES = 1_000_000
_READ_MAX_BYTES = 200_000
# git 输出上限：diff 远超旧值 4000（实测场景2 的 diff 约 4.2k 字符，恰好被截掉头部）
_GIT_OUTPUT_LIMIT = 40_000
# 测试输出上限（保留尾部：gradle 结论在末尾）
_TEST_OUTPUT_LIMIT = 8_000

# git 子命令白名单（§4.6：无 pull/fetch/reset——run 期间禁止漂移）
_GIT_ALLOWED = {"status", "diff", "add", "commit", "push", "rev-parse", "branch", "checkout", "log"}


class WorkspaceToolError(RuntimeError):
    """工作区工具越界/前置条件不满足。"""


def _workspace_root() -> Path:
    from ..config import get_settings

    return Path(get_settings().workspace_root)


def _resolve_repo(service: str) -> Path:
    """解析本次 run 的 service 工作区；越界或未 prepare 即拒绝。"""
    run_id = current_run.get()
    if not run_id:
        raise WorkspaceToolError(
            "无当前 run 上下文（工作区工具只能在 DAG 节点执行期使用）"
        )
    tenant = current_tenant.get() or "local"
    if not service or "/" in service or service.startswith("."):
        raise WorkspaceToolError(f"非法 service 名: {service!r}")

    wm = WorkspaceManager(tenant, run_id, workspace_root=_workspace_root())
    repo = wm.get_workspace(service)
    if not (repo / ".git").exists():
        raise WorkspaceToolError(
            f"service={service!r} 在本次 run 未 prepare（工作区不存在: {repo}）"
        )
    return repo


def _safe_path(repo: Path, rel: str) -> Path:
    """工作区内相对路径解析（realpath 前缀校验，堵 ../ 逃逸 + .git 写入）。"""
    if not rel or rel.startswith("/"):
        raise WorkspaceToolError(f"路径必须是工作区内相对路径: {rel!r}")
    root = repo.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise WorkspaceToolError(f"路径越界（超出工作区）: {rel!r}")
    if ".git" in target.relative_to(root).parts:
        raise WorkspaceToolError("禁止读写 .git 内部")
    return target


# ======================================================================
# 工具实现（签名与 mock/真实 adapter 同族，供 build_toolkit 包装）
# ======================================================================
async def ws_read_file(service: str, path: str, max_bytes: int = _READ_MAX_BYTES) -> dict:
    """读工作区内文件（工作区相对路径）。"""
    repo = _resolve_repo(service)
    target = _safe_path(repo, path)
    if not target.is_file():
        return {"found": False, "path": path, "content": "", "summary": f"文件不存在: {path}"}
    data = target.read_bytes()[:max_bytes]
    return {
        "found": True, "path": path,
        "content": data.decode("utf-8", "replace"),
        "summary": f"读取 {path}（{len(data)} bytes）",
    }


async def ws_list_files(service: str, path: str = "", pattern: str = "**/*") -> dict:
    """列出工作区内文件（相对路径，最多 200 条）。"""
    repo = _resolve_repo(service)
    base = _safe_path(repo, path) if path else repo
    if not base.exists():
        return {"files": [], "summary": f"路径不存在: {path}"}
    files = [
        str(p.relative_to(repo))
        for p in sorted(base.glob(pattern))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    ][:200]
    return {"files": files, "count": len(files), "summary": f"{path or '.'} 下 {len(files)} 个文件"}


async def ws_write_file(service: str, path: str, content: str) -> dict:
    """写工作区内文件。LLM 的低成本文本编辑入口（§10.3 路径白名单）。

    ⚠️ 这是**本地实现**，直接写在 worker 进程的文件系统上。生产形态下写操作必须经
    沙箱（``ws_write_file_sandboxed``）——由 ``agents/tools.build_workspace_tools``
    在注入了 ``sandbox_client`` 时改走那一条。保留本函数是为了：
    本地 E2E 无 sidecar 时仍可跑（显式配置），以及被沙箱版复用作路径校验的单一来源。
    """
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise WorkspaceToolError(f"写入超限（>{_MAX_WRITE_BYTES} bytes）")
    repo = _resolve_repo(service)
    target = _safe_path(repo, path)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": path, "created": not existed, "bytes": len(content.encode("utf-8")),
        "summary": f"{'新建' if not existed else '覆盖'} {path}",
    }


# ----------------------------------------------------------------------
# 沙箱版（写 / 测试）：跑的是仓库代码，必须在**没有密钥**的容器里执行
# ----------------------------------------------------------------------
# 判据：谁持有密钥，谁不执行不可信代码。worker 有 DeepSeek key / DB DSN / MCP 凭证 /
# git PAT，而 `build.gradle` 本身就是可执行脚本 —— 所以写入与测试都交给沙箱 sidecar。
#
# **读**（ws_read_file / ws_list_files）刻意**不走沙箱**：诊断链的 `code-locator`
# 靠它们定位问题，如果读也依赖沙箱，沙箱一挂整条诊断链就跑不起来。读不改状态、不执行
# 仓库代码，风险低。这个取舍是有意的，别"顺手统一"。


async def ws_write_file_sandboxed(sandbox, service: str, path: str, content: str) -> dict:
    """经沙箱写工作区内文件（沙箱与 worker 挂**同一个卷**，路径两侧一致）。"""
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise WorkspaceToolError(f"写入超限（>{_MAX_WRITE_BYTES} bytes）")
    repo = _resolve_repo(service)
    target = _safe_path(repo, path)  # 沿用同一套越界校验，不另写一份
    existed = target.exists()
    try:
        await sandbox.write_file(str(target), content)
    except Exception as exc:  # noqa: BLE001 —— 统一转成工具错误，带上"沙箱不可达"的判据
        raise WorkspaceToolError(
            f"沙箱写文件失败（沙箱不可达时**不会**回退到 worker 本地执行）: {exc}"
        ) from exc
    return {
        "path": path, "created": not existed, "bytes": len(content.encode("utf-8")),
        "summary": f"{'新建' if not existed else '覆盖'} {path}（沙箱）",
    }


def _fail_closed_ws_tool(name: str, original):
    """沙箱未接线时注册的占位：**调用即报错**。

    为什么不干脆不注册这个工具：不注册的话模型看不到它，会绕开或用别的方式"想办法"，
    失败形态是**静默**的（本仓 `docs/TODO.md` §23 整节都是这类）。注册一个会明确报错的，
    失败点就落在调用处、且说的是真原因。

    保留 ``original`` 的签名：AgentScope 按签名给模型生成参数 schema，用 ``*args``
    会让模型看不到该传什么——错误就退化成"参数不对"，而不是"沙箱没接线"。
    """

    async def _stub(*_args: Any, **_kwargs: Any) -> dict:
        raise WorkspaceToolError(
            f"{name} 需要沙箱（写入与测试跑的是仓库代码，必须在无密钥的容器里执行），"
            "但当前进程未接线 sandbox_client。检查 AGENTFLOW_SANDBOX_URL 与 worker Pod 的"
            " sandbox sidecar。**不会**回退到 worker 本地执行。"
        )

    _stub.__name__ = name
    _stub.__signature__ = inspect.signature(original)  # type: ignore[attr-defined]
    return _stub


def test_cmd_for(service: str) -> str:
    """该服务的测试命令（**只能来自部署配置**，见 ``AGENTFLOW_TEST_CMDS``）。

    为什么不让 agent 传命令：原先签名是 ``ws_run_tests(service, command)``，由 LLM 传
    自由命令、再用前缀白名单去猜安不安全——而白名单里含 ``"bash "``，
    ``bash -c "<任意>"`` 直接通过，**等于没有白名单**。
    改成"可执行命令的集合在部署时定死"之后，"白名单"这件事就不存在了。

    未配置 → **报错**（fail-closed）。**不要**给默认值：一个"跑得起来"的默认命令会让
    配置漏了也照跑，正是本仓反复踩的那类静默缺陷（`docs/TODO.md` §23）。
    """
    from ..config import get_settings

    raw = (get_settings().test_cmds or "").strip()
    if not raw:
        raise WorkspaceToolError(
            f"未配置测试命令：service={service!r} 无 AGENTFLOW_TEST_CMDS 条目"
            "（该配置决定每个服务能跑什么命令，不再接受调用方传参）"
        )
    try:
        table = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkspaceToolError(f"AGENTFLOW_TEST_CMDS 不是合法 JSON: {exc}") from exc
    cmd = (table or {}).get(service)
    if not cmd:
        raise WorkspaceToolError(
            f"service={service!r} 未在 AGENTFLOW_TEST_CMDS 中配置测试命令"
            f"（已配置的服务：{sorted((table or {}))}）"
        )
    return cmd


async def ws_run_tests(service: str, timeout: int = 120) -> dict:
    """在工作区执行**该服务配置的**测试命令。

    ⚠️ **无 command 参数**——命令来自部署配置（``test_cmd_for``），不接受 LLM 传参。
    这是"测试命令不来自模型"这条要求的落点。

    本函数是**本地实现**。生产形态经 ``ws_run_tests_sandboxed`` 走沙箱 sidecar
    （``build_workspace_tools`` 注入 ``sandbox_client`` 时自动切换）。
    """
    repo = _resolve_repo(service)
    cmd = test_cmd_for(service)
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
        timed_out = False
    except TimeoutError:
        proc.kill()
        out, rc, timed_out = b"(timeout)", -1, True
    return _test_result(cmd, rc, timed_out, out.decode("utf-8", "replace"), where="本地")


async def ws_run_tests_sandboxed(sandbox, service: str, timeout: int = 120) -> dict:
    """经沙箱执行**该服务配置的**测试命令（跑的是仓库代码，必须在无密钥容器里）。"""
    repo = _resolve_repo(service)
    cmd = test_cmd_for(service)
    try:
        res = await sandbox.run_shell(cmd, cwd=str(repo), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceToolError(
            f"沙箱执行测试失败（沙箱不可达时**不会**回退到 worker 本地执行）: {exc}"
        ) from exc
    return _test_result(
        cmd, res.rc, res.timed_out, (res.stdout or "") + (res.stderr or ""), where="沙箱"
    )


def _test_result(cmd: str, rc: int, timed_out: bool, text: str, *, where: str = "") -> dict:
    """两个实现（本地 / 沙箱）共用同一套结果组装——**别让它们漂移**。

    测试输出保留**尾部**：gradle 的结论（BUILD SUCCESSFUL/FAILED）在末尾，
    留头部等于把最该看的那行截掉。截断处显式标注，模型才知道自己看的是残缺内容。
    """
    if len(text) > _TEST_OUTPUT_LIMIT:
        omitted = len(text) - _TEST_OUTPUT_LIMIT
        text = f"... [前 {omitted} 字符已省略] ...\n" + text[-_TEST_OUTPUT_LIMIT:]
    return {
        "passed": rc == 0 and not timed_out,
        "rc": rc, "timed_out": timed_out,
        "output": text,
        "summary": f"`{cmd}` → rc={rc}{where}"
                   + ("（超时）" if timed_out else ""),
    }


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """超长输出保留**头部**并显式标注截断。

    git diff 的头部是元信息（``diff --git a/... b/...`` + ``@@ -old,+new @@``），
    保留尾部会把它们切掉，模型拿到无头 diff 只能自行编造——实测 fix-implementer
    据此上报了一段伪造的 ``index 0000000..1111111`` / ``@@ -1,12 +1,12 @@``。
    截断必须显式可见（哨兵文本），否则模型无从知道自己看到的是残缺内容。
    """
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return f"{text[:limit]}\n... [输出被截断，省略 {omitted} 字符] ...", True


async def ws_git(service: str, args: list[str], message: str = "", remote: str = "origin") -> dict:
    """在工作区执行 git 子命令（白名单 + 版本冻结语义，§4.6/§8.7）。

    ``pull``/``fetch``/``reset`` 不在白名单——run 期间工作区 HEAD 必须等于 base_sha。
    """
    repo = _resolve_repo(service)
    if not args:
        raise WorkspaceToolError("git 参数为空")
    sub = args[0]
    if sub not in _GIT_ALLOWED:
        raise WorkspaceToolError(
            f"git 子命令不在白名单: {sub!r}（禁止 pull/fetch/reset——§4.6 版本冻结）"
        )
    if sub == "commit" and not message:
        raise WorkspaceToolError("git commit 需要 message")

    # ⚠️ `core.hooksPath=/dev/null`：**禁用仓库自带的 hook**。
    #
    # 仓库内容是不可信的（沙箱写进去的、来自工单定位到的代码），而 `.git/hooks/`
    # 也在工作区里、同样可写。`git commit` 会执行 `pre-commit` / `commit-msg`，
    # `git push` 会执行 `pre-push` —— 恶意仓库因此能在 **worker 容器里**执行任意代码，
    # 而 worker 持有 DeepSeek key / DB DSN / MCP 凭证 / git PAT。这条正好绕过
    # "写和测试进沙箱"的隔离，所以必须堵。
    #
    # 用 `core.hooksPath` 而不是 `--no-verify`：后者只跳过 commit 的两个 hook，
    # 挡不住 `pre-push` 等其余钩子。`-c` 是全局选项，**必须排在子命令之前**。
    full = ["git", "-c", "core.hooksPath=/dev/null", *args]
    proc = await asyncio.create_subprocess_exec(
        *full, cwd=str(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise WorkspaceToolError(f"git {' '.join(args)} 失败: {text[:300]}")
    body, truncated = _truncate(text, _GIT_OUTPUT_LIMIT)
    return {
        "rc": proc.returncode, "output": body, "truncated": truncated,
        "summary": f"git {' '.join(args)} → ok" + ("（输出已截断）" if truncated else ""),
    }


# ======================================================================
# Tool Registry 元数据（写入 tools.TOOL_REGISTRY 由调用方完成）
# ======================================================================
WORKSPACE_TOOL_AGENTS: dict[str, list[str]] = {
    "ws_read_file": ["fix-implementer", "tester", "reviewer", "root-cause", "code-locator"],
    "ws_list_files": ["fix-implementer", "tester", "reviewer", "code-locator"],
    "ws_write_file": ["fix-implementer"],
    "ws_run_tests": ["tester", "fix-implementer"],
    "ws_git": ["committer", "fix-implementer"],
}

WORKSPACE_TOOLS: dict[str, Any] = {
    "ws_read_file": ws_read_file,
    "ws_list_files": ws_list_files,
    "ws_write_file": ws_write_file,
    "ws_run_tests": ws_run_tests,
    "ws_git": ws_git,
}
