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
from pathlib import Path
from typing import Any

from ..exec_context import current_run, current_tenant
from ..workspace.manager import WorkspaceManager

_MAX_WRITE_BYTES = 1_000_000
_READ_MAX_BYTES = 200_000

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
    """写工作区内文件。LLM 的低成本文本编辑入口（§10.3 路径白名单）。"""
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


async def ws_run_tests(service: str, command: str | None = None, timeout: int = 120) -> dict:
    """在工作区执行测试命令（默认 gradle 单测）。

    刻意不走沙箱：本地 E2E 环境无沙箱 Pod 常驻（M4 沙箱见 sandbox/）。命令受白名单
    前缀约束，且 cwd 固定在工作区内。
    """
    repo = _resolve_repo(service)
    cmd = command or "./gradlew test --no-daemon -q"
    allowed_prefixes = ("./gradlew", "gradle", "mvn ", "mvn\t", "python ", "pytest", "npm ", "bash ")
    if not cmd.startswith(allowed_prefixes):
        raise WorkspaceToolError(f"测试命令不在白名单内: {cmd!r}")
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
    text = out.decode("utf-8", "replace")
    return {
        "passed": rc == 0, "rc": rc, "timed_out": timed_out,
        "output": text[-4000:],
        "summary": f"`{cmd}` → rc={rc}" + ("（超时）" if timed_out else ""),
    }


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

    full = ["git", *args]
    proc = await asyncio.create_subprocess_exec(
        *full, cwd=str(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise WorkspaceToolError(f"git {' '.join(args)} 失败: {text[:300]}")
    return {
        "rc": proc.returncode, "output": text[-4000:],
        "summary": f"git {' '.join(args)} → ok",
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
