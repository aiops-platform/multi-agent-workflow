"""发布工具：把本次 run 的 PR 合并到主干。目前只有 ``merger`` 一个消费方。

与 ``workspace_tools`` 并列的第四类工具（不是 MCP、不是沙箱、不是 ActionExecutor）。

**为什么它留在 worker 进程里**（三条，前两条是硬的）：

1. **上下文**：工作区靠 ``exec_context.current_run`` / ``current_tenant`` 定位 ——
   那是进程内的 contextvar，由 DAGExecutor 在节点执行前置位。MCP server 是另一个进程，
   拿不到它，只能靠调用方把 run_id 传进去，而"钉死在本次 run"这个性质正是我们要的。
2. **凭证**：``gh`` 自己从 keychain 取 token，我们不读、不转发、不落任何 GitHub 令牌
   （§9.7 / §24）—— worker 进程里因此不存在一个会被日志/异常/``repr`` 带出去的 PAT 变量。
   下沉到 MCP 就意味着把 token 放进 ``mcp_servers.config.headers``（那个字段现在还是
   明文存储 + GET 回显，见 ``docs/TODO.md`` §2）。
3. 加一个 MCP 工具要走跨仓改 + 重部署 + 每个租户补一行绑定，而**绑定缺行的症状是
   "agent 零工具、模型把工具调用写成纯文本、最后报未输出合法 JSON"**，中间没有一步
   会说"绑定缺失"。

**用 `gh api` 而不是 `gh pr merge`**：`PUT .../pulls/{n}/merge` 一次调用就返回
``{"merged": true, "sha": "<主干上那个合并提交>"}``。``gh pr merge`` 只打印一段人话，
还得再补一次 ``gh pr view --json mergeCommit``：多一次调用、多一个竞态窗口，
且那段话要正则解析。
"""
from __future__ import annotations

import json
import re
import shutil
from typing import Any

from .workspace_tools import (
    WorkspaceToolError,
    _git_argv,
    _parse_github_remote,
    _require_github_origin,
    _resolve_repo,
    _run,
)

#: GitHub PR 链接 → (owner, repo, number)。**只认 github.com**。
_PR_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)$"
)

#: 合并方式**不暴露成参数**：让模型挑 ``--merge`` / ``--rebase`` 等于让它选不可逆历史的
#: 形状，与"让它挑 base"是同一件事。squash 还有个好处：主干保持线性。
_MERGE_METHOD = "squash"


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """解析 ``https://github.com/<owner>/<repo>/pull/<n>``；不是这个形状就报错。"""
    m = _PR_URL_RE.match((pr_url or "").strip().rstrip("/"))
    if m is None:
        raise WorkspaceToolError(
            f"commit 给出的 pr_url 不是 GitHub PR 链接：{pr_url!r}（"
            "期望形如 https://github.com/<owner>/<repo>/pull/<n>）——"
            "本工具不接受 PR 号这种跨仓有歧义的标识"
        )
    return m.group("owner"), m.group("repo"), int(m.group("number"))


async def ws_merge_pr(service: str, pr_url: str) -> dict:
    """把 ``pr_url`` 指向的那个 PR 合并到主干（squash），返回合并提交。

    ## 输入来自 commit 的输出，但**每一步都核**

    ``pr_url`` 由工作流从 ``$.nodes.commit.output.pr_url`` 喂进来 —— 图上已经有现成的
    PR 链接，工具不该装作看不见去自己反查分支（那是把显式事实换成隐式推断）。
    但也**不能盲信**它：本仓 main 上堆着多个来自历史 run 的未合并 PR，一个填错/编造的
    链接会去合别人的分支，而 ``gh`` 不会有任何异议。

    所以判据是「**用 commit 给的链接去定位，但要求它落在我自己的仓库 + 我自己的分支 +
    我自己那个 commit 上**」，三者任一不符就停。

    ⚠️ 顺带这也让 commit 那句"我开了 PR"变成**可核对的**，而不只是被信 ——
    与 §3.3「声称改了 ≠ 真改了」同族，判据从"沙箱里有没有那次成功写入"
    换成"远端那个 PR 的头是不是我这个 commit"。

    ## 幂等回落（`state == MERGED`）是**承重的**

    ``execute_with_idempotency`` 的缓存只在 agent **吐出合法 JSON** 时才记成功。
    若 agent 已经合成功了、却在收尾时失败（``AgentOutputError``，本仓高频），
    那条 attempt 记的是 ``failed`` ⇒ resume 时节点重跑 ⇒ **本工具被真真切切再调一次**。
    此时 PR 已经是 MERGED —— 没有这个回落就会报"PR 找不到" → ``on_failure: abort``
    → **把已经发生的合并报成失败**。
    判据：**这个工具重跑一次，外部世界不该再多一次可见的变化，
    也不该把一个已经发生的动作报成失败。**
    """
    repo = _resolve_repo(service)

    # 缺 gh 时别让它退化成 FileNotFoundError 冒到 runner —— 那条路径的报错
    # 与"工具坏了"长得一样，看不出是环境缺件（§9.7：换机器先 make doctor）。
    if shutil.which("gh") is None:
        raise WorkspaceToolError(
            "本机找不到 gh CLI，无法合并 PR。装法见 docs/constraints/09.7-pr-gh.md，"
            "或跑 `make doctor --install`"
        )

    owner, name, number = _parse_pr_url(pr_url)

    # origin 必须也是同一个 GitHub 仓库：否则"commit 说 PR 在 A，我却站在 B 上"。
    # （`_require_github_origin` 已保证 origin 非空且能解析，这里只做一致性比对。）
    origin = await _require_github_origin(repo)
    if _parse_github_remote(origin) != (owner, name):
        raise WorkspaceToolError(
            f"commit 说 PR 在 {owner}/{name}，可本次 run 的工作区 origin 是 {origin} —— "
            "拒绝合并不是这个工作区的 PR"
        )

    # `--repo` 显式给定，**不靠 gh 自己从远端推断**：一个工作区可以有多个 remote
    # （本机 servers/ 那份副本就同时挂过 origin 与 fork），让 gh 去猜是没必要的风险。
    view = await _run(repo, [
        "gh", "pr", "view", str(number), "--repo", f"{owner}/{name}",
        "--json", "state,headRefName,headRefOid,mergeCommit,mergeStateStatus",
    ])
    try:
        info = json.loads(view.strip())
    except json.JSONDecodeError as exc:
        raise WorkspaceToolError(f"gh pr view 未返回合法 JSON: {view[:300]}") from exc

    state = (info.get("state") or "").upper()
    head_ref = info.get("headRefName") or ""
    head_oid = info.get("headRefOid") or ""

    # ---- 幂等回落：已经合过了就如实说，**不再合一次** ----
    if state == "MERGED":
        merged_sha = (info.get("mergeCommit") or {}).get("oid") or ""
        return {
            "merged": True, "already_merged": True, "pr_url": pr_url,
            "pr_number": number, "merge_commit": merged_sha,
            "merge_method": _MERGE_METHOD, "head_ref": head_ref,
            "summary": f"PR #{number} 早已合并（{merged_sha[:12] or '合并提交未知'}），未重复合并",
        }

    if state != "OPEN":
        raise WorkspaceToolError(
            f"PR #{number} 当前状态是 {state}（只有 OPEN 才能合并），拒绝操作"
        )

    # 钉死"我合的就是我提交的那个 commit"。两条 git 都走 `_git_argv`（与 ws_git 同一份）——
    # 那两个 `-c` 是安全项，手写一份出来就会漏掉其中一个（`ws_open_pr` 里就只带了 hooks 那条）。
    branch = (await _run(repo, _git_argv(["rev-parse", "--abbrev-ref", "HEAD"]))).strip()
    head = (await _run(repo, _git_argv(["rev-parse", "HEAD"]))).strip()
    if head_ref and head_ref != branch:
        raise WorkspaceToolError(
            f"PR #{number} 的源分支是 {head_ref}，而工作区在 {branch} —— 不是本次 run 的分支"
        )
    if head_oid and head_oid != head:
        raise WorkspaceToolError(
            f"PR #{number} 的头是 {head_oid[:12]}，工作区 HEAD 是 {head[:12]} —— "
            "要合并的与我手里的不是同一份代码，拒绝"
        )

    # 主干动过就会冲突。这里提前报一句人能看懂的话，而不是把 gh 的 405 原样抛出来。
    #
    # ⚠️ **冲突态的取值是 `DIRTY`，不是 `CONFLICTING`**（2026-09-24 对着真实 PR 测出来的）：
    # `mergeStateStatus` 的枚举是 BEHIND / BLOCKED / CLEAN / DIRTY / DRAFT / HAS_HOOKS /
    # UNKNOWN / UNSTABLE —— `CONFLICTING` 是 **GraphQL `mergeable`** 的取值，两者不是一套。
    # 按 `CONFLICTING` 写这条守卫**永远不会触发**，而它看起来完全正常（实测本仓 PR #11
    # 就是 DIRTY）。这种"守卫在、但它不响"的形态正是本仓最贵的一类缺陷。
    #
    # `UNKNOWN` 是"GitHub 还没算完合并性"（刚推完很常见，实测几个老 PR 也是 UNKNOWN）：
    # 那种放行，由 `gh api` 自己裁决 —— 合并不成它会以非零退出、报 405，
    # 而 `_run` 会把 gh 的 stderr 原样带出来。
    if (info.get("mergeStateStatus") or "").upper() == "DIRTY":
        raise WorkspaceToolError(
            f"PR #{number} 与主干冲突（mergeStateStatus=DIRTY），"
            "需人工 rebase 后重试 —— 主干已被别的合并推进过"
        )

    resp = await _run(repo, [
        "gh", "api", "-X", "PUT", f"repos/{owner}/{name}/pulls/{number}/merge",
        "-f", f"merge_method={_MERGE_METHOD}",
    ])
    try:
        body = json.loads(resp.strip())
    except json.JSONDecodeError as exc:
        raise WorkspaceToolError(f"gh api merge 未返回合法 JSON: {resp[:300]}") from exc
    if not body.get("merged"):
        raise WorkspaceToolError(
            f"合并未生效：{body.get('message') or json.dumps(body, ensure_ascii=False)[:200]}"
        )

    sha = body.get("sha") or ""
    return {
        "merged": True, "already_merged": False, "pr_url": pr_url,
        "pr_number": number, "merge_commit": sha, "merge_method": _MERGE_METHOD,
        "head_ref": head_ref,
        "summary": f"已合并 PR #{number}（squash）→ {sha[:12] or '合并提交未知'}",
    }


# ======================================================================
# Tool Registry 元数据（写入 tools.TOOL_REGISTRY 由调用方完成）
# ======================================================================
RELEASE_TOOLS: dict[str, Any] = {
    "ws_merge_pr": ws_merge_pr,
}
