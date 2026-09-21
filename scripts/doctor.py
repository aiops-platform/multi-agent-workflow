"""环境体检 + **可按需安装** —— 换一台机器时先跑这个。

    ./venv/bin/python scripts/doctor.py            # 只体检，把每项的修复命令打出来
    ./venv/bin/python scripts/doctor.py --install  # 能自动装的直接装上（目前只有 gh CLI）
    make doctor / make doctor INSTALL=1

## 为什么需要一个"先跑这个"

本系统依赖一批**机器相关**的外部件：postgres / kafka / redis / 沙箱 / **gh CLI**。
它们的共同点是 —— **缺了不报错**，只在某条 run 跑到某一步时表现为"结果不对"：

| 缺什么 | 症状 |
|---|---|
| postgres / kafka | run 发不出去，或跑到一半失败 |
| **沙箱** | `ws_write_file` / `ws_run_tests` fail-closed —— 修复不落盘、测试一条不跑，而 tester 只能如实报 `passed: false`（实测 run_668981c0a7） |
| **gh CLI / 凭证** | `ws_open_pr` 起不来 → `commit` 节点失败 → `on_failure: abort` 让整条 run 中止 → **它下游的 `ticket-done` 根本不执行**，原系统那边什么都收不到 |

最后一行是最阴的：不是"报了个失败"，而是**闭环彻底没有回音**。

## 为什么"安装"与"凭证"要分开对待

工具（gh 本体）**可以自动装** —— 那是一条确定的命令。凭证**装不了**：它要么需要
一个人交互登录（`gh auth login`），要么需要有人把 PAT 交出来。所以 `--install`
只装工具，凭证只**打印确切的下一步**，不替人做决定。

## 判据只有一份

基础设施那几项复用 `agentflow.tenantctl._env_preflight`（provision 时用的是同一份），
gh 那项复用 `gh_preflight`。这里只负责**呈现**与**安装**，不重新判定。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.config import get_settings
from agentflow.tenantctl import _env_preflight, gh_preflight


def _run(argv: list[str]) -> bool:
    """跑一条安装命令，**先把命令打出来**（不让人对着一段黑箱等）。"""
    print(f"    $ {' '.join(argv)}")
    try:
        r = subprocess.run(argv, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"    ✗ 起不来：{exc}")
        return False
    if r.returncode != 0:
        print(f"    ✗ 退出码 {r.returncode}")
        return False
    print("    ✓ 完成")
    return True


def install_gh() -> bool:
    """按平台装 gh CLI。

    只在**确实没有** gh 时调用。Linux 上 `apt-get install gh` 多数发行版装不到
    （gh 不在默认源里）—— 那种情况下面的 fix 提示会给出官方安装页，所以这里
    失败也不算死路。
    """
    if shutil.which("gh"):
        print("  已安装，跳过")
        return True
    if sys.platform == "darwin":
        if not shutil.which("brew"):
            print("  没有 brew，装不了。见 https://cli.github.com")
            return False
        return _run(["brew", "install", "gh"])
    for pm, argv in (
        ("apt-get", ["sudo", "apt-get", "install", "-y", "gh"]),
        ("dnf", ["sudo", "dnf", "install", "-y", "gh"]),
        ("apk", ["sudo", "apk", "add", "gh"]),
    ):
        if shutil.which(pm):
            return _run(argv)
    print("  认不出包管理器。见 https://cli.github.com 的手动安装步骤")
    return False


#: gh 的凭证**装不了**：要么人交互登录，要么有人把 PAT 交出来。
#: 两种都给出来，让跑这个脚本的人（或另一个 Claude Code）自己选。
GH_CREDENTIAL_HELP = """  凭证无法自动配置，二选一：
    ① 交互式（本机开发）    gh auth login
    ② 无人值守 / 容器 / CI  设 GH_TOKEN=<PAT>（需 repo scope）
       —— gh 自己会读这个环境变量，不需要本平台做任何事；
          容器形态要把它挂进 worker 的 env（见 deploy/worker-deployment.yaml）"""


def main() -> int:
    ap = argparse.ArgumentParser(description="agentflow 环境体检 / 安装")
    ap.add_argument("--install", action="store_true", help="把能自动装的装上（gh CLI）")
    args = ap.parse_args()

    settings = get_settings()
    print("== agentflow 环境体检 ==")

    # ① gh 先看一眼"在不在"，因为 --install 要在体检**之前**把它补上，
    #    否则这次体检报的问题会在装完之后立刻过期 —— 让人白改一轮。
    if args.install and not shutil.which("gh"):
        print("\n[安装] gh CLI 不在，尝试安装")
        install_gh()

    print(f"\n  workspace_root : {settings.workspace_root}")
    print(f"  state_store    : {settings.state_store}")
    print(f"  queue          : {settings.queue}")
    print(f"  run_mode       : {settings.run_mode}")

    problems = _env_preflight(settings)

    # ② DeepSeek key 不在 _env_preflight 里（它不阻塞"环境能否起来"，
    #    只决定 agent 是走真模型还是 ScriptedJsonModel）—— 但换机器时最常忘。
    if not settings.deepseek_api_key.get_secret_value():
        problems.append(
            "未配置 DeepSeek Key（DEEPSEEK_API_KEY）→ agent 会退化成 ScriptedJsonModel "
            "（确定性桩），run 能跑完但结论无意义"
        )

    if not problems:
        print("\n✓ 环境就绪（postgres / kafka / 沙箱 / gh 均可用）")
        return 0

    print("\n⚠ 发现以下问题：")
    for i, p in enumerate(problems, 1):
        print(f"  {i}. {p}")

    # 只有**凭证**问题才打那段说明 —— "没装 CLI" 的下一步是装，不是登录
    # （判据复用 gh_preflight，不在这里重新判一次）
    if any("未登录" in p for p in gh_preflight()):
        print("\n[gh 凭证]")
        print(GH_CREDENTIAL_HELP)

    print("\n修复后重跑：make doctor")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
