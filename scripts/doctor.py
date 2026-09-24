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
| **沙箱镜像** | compose 的 sandbox 服务**只声明 image、没有 build** → 起不来。**报错看着像网络/权限问题**（pull access denied），而要做的是 `make sandbox-image` |
| **沙箱** | `ws_write_file` / `ws_run_tests` fail-closed —— 修复不落盘、测试一条不跑，而 tester 只能如实报 `passed: false`（实测 run_668981c0a7） |
| **gh CLI / 凭证** | `ws_open_pr` 起不来 → `commit` 节点失败 → `on_failure: abort` 让整条 run 中止 → **它下游的 `ticket-done` 根本不执行**，原系统那边什么都收不到 |

最后一行是最阴的：不是"报了个失败"，而是**闭环彻底没有回音**。

## 为什么"安装"与"凭证"要分开对待

工具（gh 本体）**可以自动装** —— 那是一条确定的命令。凭证**装不了**：它要么需要
一个人交互登录（`gh auth login`），要么需要有人把 PAT 交出来。所以 `--install`
只装工具，凭证只**打印确切的下一步**，不替人做决定。

## 判据只有一份

**已有**的判据不在这里重写：

- 基础设施那几项复用 `agentflow.tenantctl._env_preflight`（provision 时用的是同一份）
- gh 那项复用 `gh_preflight`

这里只负责**呈现**与**安装**。

## 唯一的例外：工具**版本**基线

`check_toolchain()` 是本脚本**自带**的判据，因为它查的问题与上面两者正交：

| | 查什么 | 判据来源 |
|---|---|---|
| `_env_preflight` | 服务/CLI **在不在** | tenantctl（provision 的同一份） |
| `gh_preflight` | 凭证**登没登** | tenantctl |
| **`check_toolchain`** | 工具**版本对不对** | **`toolchain.toml`** |

之所以必须单列：**版本漂移是静默的**。本仓实测 —— dev 依赖里 `ruff>=0.5` 无上界，
一次 `pip install` 把它换成 0.16.4，默认规则集从 4 条族膨胀到 ≈788 条，
于是 `make lint` 凭空多出 19 个错误**并且一直是红的**，没有任何提示。
"在不在"这类检查永远抓不到这种问题，只有比对版本才抓得到。
"""
from __future__ import annotations

import argparse
import re
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


def check_toolchain() -> tuple[list[str], list[str]]:
    """把**实际工具版本**与 `toolchain.toml` 的声明比对。返回 ``(problems, advisories)``。

    `problems` 影响退出码（环境没就绪）；`advisories` 只打印、不影响退出码。

    ## 为什么值得单独一项

    版本差异是**静默**的 —— 这是本仓反复踩的那族。最近一次实例：dev 依赖里
    `ruff>=0.5` 这个无上界约束，让一次 `pip install` 把 ruff 换成 0.16.4，
    而它的默认规则集从 4 条族膨胀到 ≈788 条 → `make lint` 凭空多出 19 个错误
    **并一直是红的**。全程没有任何提示，直到有人去查它为什么不绿。

    这里把那件事变成一次可比对的检查。

    ## 判据的边界

    - 只查**工具**（CLI 可执行文件）。依赖锁定是另一件事（retro §7.2 E1/E2/E3）。
    - `toolchain.toml` 不在 → 静默返回（不阻塞），因为这个文件是**新增**的，
      旧 checkout 上没有它不该被当成"环境坏了"。
    ## 三档语义（**别把第三档写成第二档**）

    | 声明 | 缺失时 | 版本不符时 | 影响退出码 |
    |---|---|---|---|
    | （默认）必需 | 报 problem | 报 problem | ✅ 是 |
    | `optional = true` | **不提** | 报 problem | 仅版本不符时 |
    | **`advisory = true`** | **打印提示** | **打印提示** | ❌ **否** |

    `advisory` 这一档是为「值得知道、但缺了不算环境坏了」的工具准备的 ——
    例如 codegraph：不装它一切照常（`.claude/CLAUDE.md` 指令里有"没索引就跳过"的兜底），
    但**新队友应该在第一次跑 doctor 时就知道有这么个东西、以及一条命令怎么装**。
    写成 `optional` 会静默不提（达不到告知目的），写成必需又会把没装它的人挡在门外。
    """
    import tomllib
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    root = Path(__file__).resolve().parent.parent
    cfg_path = root / "toolchain.toml"
    if not cfg_path.exists():
        return [], []
    try:
        tools = tomllib.loads(cfg_path.read_text(encoding="utf-8")).get("tools", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"toolchain.toml 读不出来（{exc}）—— 工具版本基线这次没有比对"], []

    problems: list[str] = []
    advisories: list[str] = []
    for name, spec in tools.items():
        cmd = spec.get("cmd") or []
        required = spec.get("required") or ""
        note = spec.get("note") or ""
        optional = bool(spec.get("optional"))
        advisory = bool(spec.get("advisory"))
        # advisory 的项：提示语单独攒，不进 problems
        sink = advisories if advisory else problems
        # ⚠️ 缺失时**只挡 `optional`**，不挡 `advisory` ——
        # advisory 存在的全部理由就是"缺失时要出声"。初版把这里写成
        # `optional or advisory`，结果它被自己挡住、一声不吭，
        # 而"缺失"恰恰是它唯一的用武之地（实测：模拟新队友没装 → 零输出）。

        if not cmd or not required:
            continue

        def _unavailable(why: str) -> None:
            """命令拿不到版本时的统一出口。

            ⚠️ 两条路径都要覆盖：**可执行文件本身不存在**（FileNotFoundError），
            与 **`sh -c` 包了一层、但里面那条命令不存在**（sh 自己 rc=0，
            错误只在 stderr 里）。只判前者会让后者落进"认不出版本号"，
            对**最该说清楚的那个场景**（新队友没装）吐一句 `sh: ... No such file`。
            """
            text = f"{name}：{why}" + (f" —— {note}" if note else "")
            (advisories if advisory else problems).append(text)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            raw = f"{r.stdout}\n{r.stderr}"
        except (OSError, subprocess.SubprocessError):
            if not optional:
                _unavailable("跑不起来")
            continue

        m = re.search(r"\d+\.\d+(?:\.\d+)?", raw)
        if not m:
            if r.returncode != 0:
                # 命令在，但跑不出来（或 sh -c 里的目标不存在）→ 当作"没装/不可用"
                if not optional:
                    _unavailable("没装")
            else:
                sink.append(f"{name}：认不出版本号（原始输出：{raw.strip()[:80]!r}）")
            continue
        actual = m.group(0)

        try:
            ok = SpecifierSet(required).contains(actual)
        except InvalidSpecifier:
            problems.append(f"{name}：toolchain.toml 里的约束 {required!r} 不是合法 PEP 440")
            continue

        if not ok:
            sink.append(
                f"{name}：实际 {actual}，基线要求 {required}"
                + (f" —— {note}" if note else "")
                + "（要么升工具，要么改 toolchain.toml 的基线 —— **别让它悄悄漂着**）"
            )
    return problems, advisories


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

    # ③ 工具链版本基线（retro §7.2 E2 / §7.4 E4）。
    #    与 _env_preflight 查的"服务在不在"正交：那查**有没有**，这查**版本对不对**。
    #    版本漂移是静默的（本仓实测：ruff 被无上界约束换掉后 lint 无声变红），
    #    所以它必须在"换机器先跑这个"里被挡住。
    toolchain_problems, advisories = check_toolchain()
    problems.extend(toolchain_problems)

    # ② DeepSeek key 不在 _env_preflight 里（它不阻塞"环境能否起来"，
    #    只决定 agent 是走真模型还是 ScriptedJsonModel）—— 但换机器时最常忘。
    if not settings.deepseek_api_key.get_secret_value():
        problems.append(
            "未配置 DeepSeek Key（DEEPSEEK_API_KEY）→ agent 会退化成 ScriptedJsonModel "
            "（确定性桩），run 能跑完但结论无意义"
        )

    # ③ 提示项（`advisory = true`）：**不影响退出码**，但每次体检都打印。
    #    和 problems 分开，是因为它们的"下一步"不同 —— 问题要修，提示只是让你知道。
    #    codegraph 就在这里：不装它一切照常，但新队友该在这一步知道有这么个东西。
    if advisories:
        print("\nℹ 可选工具（不影响环境是否就绪）：")
        for a in advisories:
            print(f"  · {a}")

    if not problems:
        print("\n✓ 环境就绪（postgres / kafka / 沙箱镜像与沙箱 / gh 均可用）")
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
