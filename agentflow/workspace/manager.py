# -*- coding: utf-8 -*-
"""WorkspaceManager（design §8.7 + §4.6 Git 版本冻结）。

核心约束：
1. **base_sha 冻结**：Run 创建时确定每个仓库的 base_sha；prepare 只 clone 到该 SHA。
2. **禁止 run 中 git_pull**：本模块**不提供 pull 方法**（§8.7.2 明示）。已存在的工作区
   只做 HEAD == base_sha 校验（幂等），不一致时报错，绝不拉新。
3. **分支隔离**：每个 Run 创建独立分支 ``aiops/RUN_{run_id}``（§8.7.3），并发 Run 互不干扰。
4. 工作区布局（§8.7.2）：``/workspace/{tenant_id}/{run_id}/repos/{service}``。

本地联调用 ``file://`` 源仓库（与 S-009 spike 一致）；生产支持 https + PAT（日志脱敏）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Iterable

from .models import RepoSpec, workspace_repo_path

log = logging.getLogger("agentflow.workspace")

_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}\tHEAD$")


class GitError(RuntimeError):
    pass


class FrozenVersionMismatch(GitError):
    """工作区已存在但 HEAD != base_sha：违反版本冻结，禁止运行中 pull/reset。"""


class GitShell:
    """git 子进程封装（asyncio），便于测试注入 fake。"""

    def __init__(self, pat: str | None = None) -> None:
        self.pat = pat

    def _sanitize(self, text: str) -> str:
        if self.pat:
            text = text.replace(self.pat, "***")
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)

    def _url_with_pat(self, url: str) -> str:
        if self.pat and "://" in url and "@" not in url:
            scheme, _, rest = url.partition("://")
            return f"{scheme}://{self.pat}@{rest}"
        return url

    async def run(self, args: list[str], *, cwd: Path | None = None) -> str:
        cmd = ["git", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = (stdout + stderr).decode(errors="replace")
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} 失败: {self._sanitize(out)}")
        return out

    async def remote_head_sha(self, url: str) -> str:
        """解析远程 HEAD 的 sha（base_sha 未指定时冻结用）。"""
        out = await self.run(["ls-remote", self._url_with_pat(url), "HEAD"])
        for line in out.splitlines():
            if _HEAD_SHA_RE.match(line):
                return line.split("\t")[0]
        raise GitError(f"无法解析远程 HEAD: {url}")

    async def head_sha(self, cwd: Path) -> str:
        out = await self.run(["rev-parse", "HEAD"], cwd=cwd)
        return out.strip()

    async def clone_to_sha(self, url: str, dest: Path, base_sha: str, branch: str) -> None:
        """clone 到指定 SHA 并创建 AI 工作分支（§8.7.2）。"""
        await self.run(["clone", "--no-checkout", self._url_with_pat(url), str(dest)])
        await self.run(["checkout", base_sha], cwd=dest)
        await self.run(["checkout", "-B", branch], cwd=dest)


class WorkspaceManager:
    def __init__(
        self,
        tenant_id: str,
        run_id: str,
        *,
        workspace_root: Path = Path("/tmp/workspace"),
        git: GitShell | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.workspace_root = Path(workspace_root)
        self.git = git or GitShell()
        self.frozen: dict[str, RepoSpec] = {}  # service -> 冻结后的 RepoSpec

    # ---- 工作区 ----
    def get_workspace(self, service: str) -> Path:
        """返回工作区路径（§8.7.2）。"""
        return workspace_repo_path(self.workspace_root, self.tenant_id, self.run_id, service)

    @property
    def run_branch(self) -> str:
        """本 Run 的 AI 工作分支（§8.7.3 并发隔离）。"""
        return f"aiops/RUN_{self.run_id}"

    # ---- prepare（幂等）----
    async def prepare(self, repos: Iterable[RepoSpec]) -> list[Path]:
        """Run 开始时：并行 prepare 所有仓库到冻结 SHA + AI 分支。"""
        return list(await asyncio.gather(*(self.prepare_one(spec) for spec in repos)))

    async def prepare_one(self, spec: RepoSpec) -> Path:
        """prepare 单个仓库。

        1. base_sha 未指定 → 从远程 HEAD 冻结（§4.6）。
        2. 工作区已存在 → 校验 HEAD == base_sha（幂等；不一致报 FrozenVersionMismatch，
           绝不 pull/reset）。
        3. 不存在 → clone 到 base_sha + 建 ``aiops/RUN_{run_id}`` 分支。
        """
        frozen = spec if spec.base_sha else spec.freeze(await self.git.remote_head_sha(spec.url))
        self.frozen[frozen.service] = frozen

        dest = self.get_workspace(frozen.service)
        if (dest / ".git").exists():
            head = await self.git.head_sha(dest)
            if head != frozen.base_sha:
                raise FrozenVersionMismatch(
                    f"{frozen.service} 工作区 HEAD={head[:12]} != base_sha={frozen.base_sha[:12]}"
                    "（版本冻结：run 期间禁止 git_pull/reset）"
                )
            log.info("[%s] %s 工作区已就绪（HEAD==base_sha，幂等）", self.run_id, frozen.service)
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        await self.git.clone_to_sha(frozen.url, dest, frozen.base_sha, self.run_branch)
        log.info("[%s] %s cloned @%s 分支=%s", self.run_id, frozen.service,
                 frozen.base_sha[:12], self.run_branch)
        return dest
