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
import os
import re
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
# 测试超时：**与 ToolSpec 注册值和 SBX_MAX_EXEC_SECONDS 三者对齐到 300**。
# 此前函数默认 120 < ToolSpec 300 —— 小的那个**静默覆盖**大的，
# 看 ToolSpec 的人会以为有 300 秒。真实 Java 构建跑几分钟很常见，
# 120 秒会把「还在编译」误报成「测试失败」。
_TEST_TIMEOUT_SEC = 300

# git 子命令白名单（§4.6：无 pull/fetch/reset——run 期间禁止漂移）
_GIT_ALLOWED = {"status", "diff", "add", "commit", "push", "rev-parse", "branch", "checkout", "log"}

#: `ws_git` / `ws_open_pr` 起的**子进程墙钟上限**（秒）。必须存在：git 与 gh 的若干路径
#: 会停在交互式输入上（编辑器 / 凭证提示 / 分页器 / gh 的"选哪个仓"），没有它 = 节点
#: **永久 running**（实测 run_63a334c90d：一个 `vi` 挂了 14 分钟）。
#: 与 `ToolSpec("ws_git", …, timeout=120)` 声明的值取齐 —— 但那个声明**全仓零消费方**
#: （见 `docs/TODO.md` §23.6），真正生效的是这里。
_SUBPROC_TIMEOUT_SEC = 120


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
    """经沙箱写工作区内文件（沙箱与 worker 挂**同一个卷**，路径两侧一致）。

    那个"同一个卷"**是被检查的前提，不是假设**（见下面写后复读）：它不成立时，沙箱会在
    **容器自己的文件系统**上把文件写得好好的、回一句 `written: true`，而 worker 侧的工作区
    一点没变 —— 于是 `ws_git` / `tester` / `committer` 看到的还是改之前的代码，全程无报错。
    """
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise WorkspaceToolError(f"写入超限（>{_MAX_WRITE_BYTES} bytes）")
    repo = _resolve_repo(service)
    target = _safe_path(repo, path)  # 沿用同一套越界校验，不另写一份
    existed = target.exists()
    try:
        await sandbox.write_file(str(target), content)
    except Exception as exc:  # noqa: BLE001 —— 统一转成工具错误，带上"沙箱不可达"的判据
        # 带上**类型名 + repr**：`str(httpx.ReadError(""))` 是空串，只写 `{exc}` 会得到
        # 「沙箱写文件失败（…）: 」——冒号后什么都没有。实测 run_fc9e158b55：agent 拿着这句
        # 反复重试 9 轮，既无从自我纠正，事后也判不出是 RST / 超时 / 还是别的。
        raise WorkspaceToolError(
            "沙箱写文件失败（沙箱不可达时**不会**回退到 worker 本地执行）"
            f"（{type(exc).__name__}: {exc!r}）"
        ) from exc
    await _verify_visible_to_worker(target, content, repo)
    return {
        "path": path, "created": not existed, "bytes": len(content.encode("utf-8")),
        "summary": f"{'新建' if not existed else '覆盖'} {path}（沙箱）",
    }


async def _verify_visible_to_worker(target: Path, content: str, repo: Path) -> None:
    """沙箱写完，**在 worker 侧复读一次**：看不到就报错，绝不报成功。

    为什么必须有这一步：工具层"成功"的判据原来只有"沙箱 HTTP 回来了"。而**两侧不是同一个
    卷**时（实测 2026-09-22：compose 只挂 `${HOME}/agentflow-workspace`，而工作区根默认是
    `/tmp/agentflow-workspace` —— 它在可写白名单里，却不在卷里），沙箱把文件写进了容器自己的
    文件系统，宿主侧什么都没变，调用方却收到一句「新建 …（沙箱）」。后果不是"报了个失败"，
    是**整条修复链看的是改之前的代码**：`test` 拿假失败、`commit` 提交空 diff。
    与 §3.2「写盘失败不许报成功」同族，只是判据从"HTTP 成功"换成"**我看得见**"。

    有界重试 2 次：写是经宿主文件系统落盘的，理论上立刻可见；重试只为兜住跨文件系统视图
    （virtiofs 属性缓存之类）的短暂滞后 —— 而这条路径上的**假失败**代价很高（`fix` 是
    `on_failure: abort`，一次误报就中止整条 run）。
    """
    want = content.encode("utf-8")
    for attempt in range(2):
        seen = target.read_bytes() if target.is_file() else None
        if seen == want:
            return
        if attempt == 0:
            await asyncio.sleep(0.05)
    raise WorkspaceToolError(
        f"沙箱写文件返回成功，但 worker 侧看不到（{target}，"
        f"期望 {len(want)} bytes，实际 {'无此文件' if seen is None else f'{len(seen)} bytes'}）"
        "—— **两侧挂的不是同一个工作区卷**：工作区根必须落在沙箱挂载的那个卷里"
        f"（compose 挂的是 ${{HOME}}/agentflow-workspace，当前仓库: {repo}）。"
        "不修的话工作区一点没变而这里报成功，下游 test/commit 全在改之前的代码上跑。"
    )


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


async def ws_run_tests(service: str, timeout: int = _TEST_TIMEOUT_SEC) -> dict:
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


async def ws_run_tests_sandboxed(sandbox, service: str, timeout: int = _TEST_TIMEOUT_SEC) -> dict:
    """经沙箱执行**该服务配置的**测试命令（跑的是仓库代码，必须在无密钥容器里）。"""
    repo = _resolve_repo(service)
    cmd = test_cmd_for(service)
    try:
        res = await sandbox.run_shell(cmd, cwd=str(repo), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        # 同 ws_write_file_sandboxed：`str(exc)` 可能是空的（httpx 的 ReadError 就是），
        # 只写 `{exc}` 等于把原因吞掉。带类型名 + repr。
        raise WorkspaceToolError(
            "沙箱执行测试失败（沙箱不可达时**不会**回退到 worker 本地执行）"
            f"（{type(exc).__name__}: {exc!r}）"
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


def _subprocess_env() -> dict[str, str]:
    """跑 git / gh 时的环境：**让一切交互式输入变成失败**。

    光给 `stdin=DEVNULL` 还不够明确 —— 这些开关是"别问，直接失败"的显式声明，
    两条一起才把"挂死"变成"响亮地失败"：

    - ``GIT_TERMINAL_PROMPT=0``：缺凭证时**不要**在终端上问用户名，直接报错。
      实测本机 `credential.helper` 为空，没这一条，第一次 `git push` over https 就会挂。
    - ``GIT_ASKPASS=true``：凡走 askpass 的路径也让它"什么都不给"。
    - ``GIT_PAGER``/``PAGER=cat``：分页器同样会停在输入上。
    - ``GH_PROMPT_DISABLED=1``：gh 的交互提示（"选哪个仓库"、确认类）一律改成失败。
      `ws_open_pr` 里有一半调用要**把 stdout 当 JSON 解析**，提示混进来就是解析崩。

    两个消费方（`ws_git`、`_run`）**共用这一份**：分开写必然漂移，而漂移的那一半
    没有任何提示 —— 本仓为"两份实现"付过代价（`_mark_cancelled`，见 CLAUDE.md §11）。
    """
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
    }


def _git_argv(args: list[str]) -> list[str]:
    """构造 git 命令行。两个 ``-c`` 都是**安全项**，谁都不能拿掉：

    - ``core.hooksPath=/dev/null``：**禁用仓库自带的 hook**。仓库内容不可信（沙箱写进去的、
      来自工单定位到的代码），而 ``.git/hooks/`` 也在工作区里、同样可写 —— 不堵就是
      "不可信仓库在持有全部密钥的 worker 里执行任意代码"。用 ``core.hooksPath`` 而不是
      ``--no-verify``：后者只跳过 commit 的两个 hook，挡不住 ``pre-push`` 等其余钩子。
    - ``core.editor=true``：任何"需要编辑器"的路径立刻以空信息失败，而不是拉起 ``vi`` 等键盘。

    ``-c`` 是全局选项，**必须排在子命令之前**。
    """
    return ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.editor=true", *args]


def _commit_message_in_args(args: list[str]) -> bool:
    """``args`` 里是否已经带了提交信息（``-m`` / ``--message`` / ``-F`` / ``--file``）。

    含 ``-m"信息"`` 这种贴在一起的写法（git 认），也含 ``--message=`` 形式。
    """
    return any(
        a in ("-m", "-F")
        or a.startswith(("-m", "--message", "-F", "--file"))
        for a in args
    )


async def ws_git(service: str, args: list[str], message: str = "") -> dict:
    """在工作区执行 git 子命令（白名单 + 版本冻结语义，§4.6/§8.7）。

    ``pull``/``fetch``/``reset`` 不在白名单——run 期间工作区 HEAD 必须等于 base_sha。

    ## 绝不允许 git 停在交互式输入上（实测 run_63a334c90d）

    那次 `commit` 节点**永久卡在 running**，进程树是
    ``uvicorn → git -c core.hooksPath=/dev/null commit → vi …/.git/COMMIT_EDITMSG``：
    调用方把提交信息填在 ``message`` 参数里（签名里有它，模型就那么填），而本函数
    **只校验、不参与构造命令**（`docs/TODO.md` §23.5）→ 真跑的是裸 ``git commit``
    → git 拉起编辑器 → ``vi`` 继承终端 stdin → ``communicate()`` 永不返回。
    节点没有流水、没有报错、run 行停在 `waiting_approval`（§28 的僵尸形态）。

    四道保险，缺一不可：
    1. ``message`` **真的接进命令**（args 里已带就不重复加）；
    2. ``-c core.editor=true``：任何"要编辑器"的路径**立刻**以空信息失败，而不是等人敲键盘；
       用命令行 ``-c``（排在子命令前）而不是改仓库配置 —— 优先级更高，恶意仓库覆盖不掉；
    3. ``stdin=DEVNULL`` + ``GIT_TERMINAL_PROMPT=0``（``push`` 缺凭证时同样会停在
       用户名提示上）+ 关掉分页器：**没有可读的终端，交互式子命令只能失败**；
    4. 有界超时（``_SUBPROC_TIMEOUT_SEC``）→ kill 并抛错：把"挂死"换成"响亮地失败"。

    ⚠️ 别把这里的 ``message`` 再改回"只校验"：那个参数的可见性来自函数签名
    （`ToolSpec` 不做参数 schema），模型**看得见就会填**。
    """
    repo = _resolve_repo(service)
    if not args:
        raise WorkspaceToolError("git 参数为空")
    sub = args[0]
    if sub not in _GIT_ALLOWED:
        raise WorkspaceToolError(
            f"git 子命令不在白名单: {sub!r}（禁止 pull/fetch/reset——§4.6 版本冻结）"
        )
    if sub == "commit" and not (_commit_message_in_args(args) or message):
        # 两种传法都收（`['commit','-m','…']` 与 `message='…'`）：签名上摆着 message，
        # 提示词里写的是 `-m`，**模型两条路都会走**，只认一条就是给自己埋雷。
        raise WorkspaceToolError(
            "git commit 需要提交信息：args 里带 `-m <信息>`，或用 `message=` 参数"
        )
    if sub == "commit" and not _commit_message_in_args(args):
        args = [*args, "-m", message]

    # 两个 `-c` 安全项的由来见 `_git_argv`。
    full = _git_argv(args)
    # stdin 给 /dev/null：git（及其拉起的编辑器/凭证提示/分页器）**没有终端可读**，
    # 遇到要输入的地方只能立刻失败 —— 这是"挂死"与"响亮地失败"之间的分水岭。
    proc = await asyncio.create_subprocess_exec(
        *full, cwd=str(repo), env=_subprocess_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_SUBPROC_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise WorkspaceToolError(
            f"git {' '.join(args)} 超时（>{_SUBPROC_TIMEOUT_SEC}s）已中止 —— "
            "该子命令多半停在交互式输入上（编辑器 / 凭证提示 / 分页器）"
        ) from None
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise WorkspaceToolError(f"git {' '.join(args)} 失败: {text[:300]}")
    body, truncated = _truncate(text, _GIT_OUTPUT_LIMIT)
    return {
        "rc": proc.returncode, "output": body, "truncated": truncated,
        "summary": f"git {' '.join(args)} → ok" + ("（输出已截断）" if truncated else ""),
    }


#: GitHub 远端 URL → (owner, repo)。三种写法都收：https / ssh:// / scp 风格 `git@host:`。
#: **不是 GitHub 的一律不匹配** —— 本地 `file://` 路径、GitLab 之类都在此被挡下。
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)


def _parse_github_remote(url: str) -> tuple[str, str] | None:
    """解析 GitHub 远端 URL → ``(owner, repo)``；不是 GitHub 远端则返回 ``None``。"""
    m = _GITHUB_REMOTE_RE.match((url or "").strip().rstrip("/"))
    return (m.group("owner"), m.group("repo")) if m else None


async def _require_github_origin(repo: Path) -> str:
    """取工作区的 origin 并要求它是**指向 GitHub 的远端**，返回该 URL。

    两个调用方共用（``ws_open_pr`` 要开 PR、``ws_merge_pr`` 要合并 PR），所以抽在这里：
    各写一份必然漂移，而漂移的那一半不会有任何提示（本仓为「两份实现」付过代价，
    见 CLAUDE.md §11 的 `_mark_cancelled`）。

    为什么是硬拦而不是"推上去再说"：本地联调形态（``AGENTFLOW_REPO_ROOT`` 指向本机
    testbed 副本）下工作区是从**本机路径**克隆的，origin 于是是 ``file:///Users/...`` ——
    推是推得动的，但推到的是**本机那份副本**，而 ``gh`` 面对它只会往 stderr 说一句
    「none of the git remotes ... point to a known GitHub host」。不先拦的话，症状是
    ``json.loads: Expecting value: line 1 column 1`` —— 既看不出是远端的问题，
    还会先在**不相干的地方**留下一个分支（实测 run_d595720e5b）。

    ⚠️ 判据必须是「**解析出来是不是 github.com**」，不能只看"有没有 ``scheme://``"：
    ``AGENTFLOW_REPO_ROOT`` 配成 GitLab 一类的 org URL 时，origin 长得完全像正常远端，
    而 ``gh`` 一样用不了 —— 那种形态比 ``file://`` 更难看出来。
    """
    origin = (await _run(repo, ["git", "remote", "get-url", "origin"], check=False)).strip()
    if not origin or _parse_github_remote(origin) is None:
        raise WorkspaceToolError(
            f"工作区的 origin 不是 GitHub 远端（{origin or '未配置'}）。"
            "本地联调时 AGENTFLOW_REPO_ROOT 指向本机副本会有这个现象；"
            "要开 PR / 合并 PR 需让工作区从真实 GitHub 远端克隆"
            "（AGENTFLOW_REPO_ROOT 用 https://github.com/<org> 形态，配完重启 API）"
        )
    return origin


async def ws_open_pr(service: str, title: str, body: str = "") -> dict:
    """把本次 run 的分支推到 origin 并**开一个 PR**，返回 ``{pr_url, pr_number}``。

    ## 为什么单独一个工具，而不是让 committer 自己拼命令

    `ws_git` 的白名单里本来就有 `push`，但**推上去不等于有 PR** —— 而
    `ticket-done` 判「已交付」靠的正是 `commit.pr_url`。此前 `committer` 的
    提示词只教了 add/commit/rev-parse 三步，输出契约却声明了 `pr_url`，
    于是那个字段**系统性地永远是空串**：每一次「修复成功」都会被回传成
    `status: failed`（实测 run_170dccffd9：fix 改了 2 个文件、test passed、
    review approved，回传的却是 failed）。

    把「推 + 开 PR」收成一个工具，是为了让那两步**同生共死**：要么真的建出
    PR，要么明确报错 —— 而不是留一个能推、不能开、还假装有 pr_url 的中间态。

    ## 自己的 `gh` 令牌不进我们的代码

    建 PR 要凭证，而 `gh` CLI **自己**从 keychain 取 token（`gh auth status`
    显示已登录）。我们不读、不转发、不落任何 GitHub 令牌 —— worker 进程里
    因此**不存在**一个会被日志/异常/`repr` 带出去的 PAT 变量（§24 密钥卫生）。
    这也是为什么它留在 worker 而不是进沙箱：**持有密钥的一方不执行不可信代码**，
    而沙箱里那份代码是不可信的。

    ## 三个边界

    - **base 不由调用方传**：取仓库自己的默认分支（`gh repo view`）。让 LLM 挑
      base 等于让它决定往哪儿合。
    - **head 取自 git 的当前分支**（工作区准备时建的 ``aiops/RUN_<run_id>``），
      同样不接受传参 —— 否则 agent 可以把**别的**分支推上去。
    - **标题/正文用 ``--title=`` 形式**：它们来自模型，若走 ``--title <值>``，
      一个以 ``-`` 开头的值会被 `gh` 当成旗标解析。
    """
    repo = _resolve_repo(service)
    title = (title or "").strip()
    if not title:
        raise WorkspaceToolError("开 PR 需要非空 title")

    branch = (await _run(repo, ["git", "-c", "core.hooksPath=/dev/null",
                                "rev-parse", "--abbrev-ref", "HEAD"])).strip()
    if not branch or branch == "HEAD":
        raise WorkspaceToolError("当前是游离 HEAD，无法作为 PR 的 head 分支")

    # ⚠️ **先确认 origin 是指向 GitHub 的远端，再推。** 守卫本体在
    # `_require_github_origin`（与 `ws_merge_pr` 共用）。
    await _require_github_origin(repo)

    # 推分支。同样禁用仓库自带的 hook —— `git push` 会执行 `pre-push`，
    # 而 `.git/hooks/` 在可写的工作区里（信任边界见 ws_git 的说明）。
    await _run(repo, ["git", "-c", "core.hooksPath=/dev/null",
                      "push", "-u", "origin", branch])

    # 幂等：同 head 已开着 PR 就复用它。committer 是副作用节点（幂等键 run_id:node_id），
    # 但**节点内的重试**仍会再次调到这里 —— 那时 gh 会因"PR 已存在"而报错，
    # 把一次本该成功的提交变成失败。
    existing = await _run(repo, ["gh", "pr", "list", "--head", branch, "--state", "open",
                                 "--json", "url,number", "--jq", ".[0]"], check=False)
    if existing.strip() and existing.strip() != "null":
        row = json.loads(existing.strip())
        return {"pr_url": row.get("url"), "pr_number": row.get("number"), "created": False,
                "branch": branch, "summary": f"PR 已存在，复用 #{row.get('number')}"}

    base = (await _run(repo, ["gh", "repo", "view", "--json", "defaultBranchRef",
                              "--jq", ".defaultBranchRef.name"])).strip()
    if not base:
        raise WorkspaceToolError("取不到仓库默认分支，拒绝猜一个 base")

    url = (await _run(repo, [
        "gh", "pr", "create", "--base", base, "--head", branch,
        f"--title={title}", f"--body={body or title}",
    ])).strip()
    pr_url = next((ln.strip() for ln in url.splitlines() if ln.strip().startswith("http")), "")
    if not pr_url:
        raise WorkspaceToolError(f"gh pr create 未返回 PR 链接: {url[:300]}")
    # PR 号从 URL 末尾取（形如 .../pull/12）
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return {"pr_url": pr_url, "pr_number": int(tail) if tail.isdigit() else 0,
            "created": True, "branch": branch, "base": base,
            "summary": f"已开 PR {pr_url}"}


async def _run(repo: Path, argv: list[str], *, check: bool = True) -> str:
    """在仓库里跑一条命令并返回 **stdout**。

    **不经 shell**（``create_subprocess_exec`` 逐个参数传）：模型给的标题/正文里
    出现 `; rm -rf` 也只是字符串，不会被解释。

    ⚠️ **stderr 单独收、不并进 stdout**。早期这里写的是 ``stderr=STDOUT``（跟
    ``ws_git`` 一样），但 ``ws_git`` 的返回是给人看的、而这里有一半调用要**当 JSON 解析**
    （``gh pr list --json``）。`gh` 会往 stderr 写提示（「这幅仓库没有指向已知 GitHub host
    的远端」就是一条），一并进来就成了"JSON 开头多了一段英文"——
    ``json.loads`` 报 ``Expecting value: line 1 column 1``，**现场完全看不出是 stderr 混进来了**。
    实测：run_d595720e5b 的 commit 节点连试三次都是这个错。

    ⚠️ **与 `ws_git` 同一套挂起保险**（2026-09-22 补，`_subprocess_env` + DEVNULL + 超时）：
    这里跑的是 `git push` 与 `gh`。缺凭证时 git 会**在终端上问用户名**（实测本机
    `credential.helper` 为空）、gh 会**问"选哪个仓库"** —— 而 stdin 一旦继承又没有超时，
    就是又一条"节点永久 running"（`ws_git` 那次是一个 `vi` 挂了 14 分钟，见 §23.5）。
    这条路径第一次真跑 `git push` over https 的时候最容易撞上。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(repo), env=_subprocess_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_SUBPROC_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise WorkspaceToolError(
            f"{' '.join(argv[:2])} 超时（>{_SUBPROC_TIMEOUT_SEC}s）已中止 —— "
            "多半停在交互式输入上（凭证提示 / gh 的选择提示）"
        ) from None
    text = out.decode("utf-8", "replace")
    detail = err.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise WorkspaceToolError(f"{' '.join(argv[:2])} 失败: {(detail or text)[:300]}")
    return text


# ======================================================================
# Tool Registry 元数据（写入 tools.TOOL_REGISTRY 由调用方完成）
# ======================================================================
WORKSPACE_TOOL_AGENTS: dict[str, list[str]] = {
    "ws_read_file": ["fix-implementer", "tester", "reviewer", "root-cause", "code-locator"],
    "ws_list_files": ["fix-implementer", "tester", "reviewer", "code-locator"],
    "ws_write_file": ["fix-implementer"],
    "ws_run_tests": ["tester", "fix-implementer"],
    "ws_git": ["committer", "fix-implementer"],
    "ws_open_pr": ["committer"],
}

WORKSPACE_TOOLS: dict[str, Any] = {
    "ws_read_file": ws_read_file,
    "ws_list_files": ws_list_files,
    "ws_write_file": ws_write_file,
    "ws_run_tests": ws_run_tests,
    "ws_git": ws_git,
    "ws_open_pr": ws_open_pr,
}
