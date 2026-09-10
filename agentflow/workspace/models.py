# -*- coding: utf-8 -*-
"""仓库规格 + Git base_sha 冻结（design §8.7.1 / §4.6）。

Run 创建时确定每个仓库的 ``base_sha`` 并冻结；整个 Run 期间不执行
``git pull``，直到 PR 提交。并发隔离：每个 Run 用独立分支 ``aiops/RUN_{run_id}``。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RepoSpec:
    service: str
    url: str  # clone 地址（支持 file:// 本地源 / https）
    base_sha: str | None = None  # 冻结版本；None 时由 prepare 从远程 HEAD 确定
    branch: str = "main"  # 目标分支（PR 合并到 main）

    def freeze(self, base_sha: str) -> "RepoSpec":
        """返回带冻结 base_sha 的副本（§4.6 版本冻结，原对象不变）。"""
        return RepoSpec(service=self.service, url=self.url, base_sha=base_sha, branch=self.branch)


# Run 级工作区布局（§8.7.2）：/workspace/{tenant_id}/{run_id}/repos/{service}
def workspace_repo_path(workspace_root, tenant_id: str, run_id: str, service: str):
    return workspace_root / tenant_id / run_id / "repos" / service
