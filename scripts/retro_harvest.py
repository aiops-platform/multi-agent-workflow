#!/usr/bin/env python3
"""回收经验素材：把一段时间内的**提交信息 / changelog / TODO 变更**扫出来，交给 LLM 三分类。

    ./venv/bin/python scripts/retro_harvest.py --since "2 weeks ago"
    make retro-harvest SINCE="2 weeks ago"

## 为什么是"回收"而不是"记录"

要求人在**当场**多写一份经验，长期必然烂尾 —— 本仓 134 个 changelog 就是证据：
产出量本身会淹没读者，而"记得写"这件事没有任何机制兜底。

**回收**利用的是**已经被强制产生的副产品**：
`CLAUDE.md` 已经强制提交信息写「为什么」，前端已强制 changelog，TODO 一直在记。
教训**已经写在里面了**，只是从没被提炼过。

## ⚠️ 这个脚本**不做判断**

三分类（→ 变检查 / → 进文档 / → 丢弃）需要判断"这条经验**泛化了吗**、**有没有判据**、
**能不能写成检查**"，那是 LLM 的活。脚本只负责**把素材收拢、去掉噪音、排出优先级**，
然后打印出**可以直接粘贴给 Claude 的指令**。

    $ make retro-harvest SINCE="2 weeks ago"
    == 素材：23 条提交 / 7 个 changelog / TODO +41 行 ==
    素材已写入 /tmp/retro-harvest-20260923.md
    下一步：把下面这段粘给 Claude Code …
        「读 docs/lessons/README.md 的收录标准，对 /tmp/retro-harvest-*.md 里的素材做三分类…」

## 凭什么是"素材"而不是"全文"

提交正文动辄几十行，全量倒出来会把主上下文吃掉 —— 所以脚本按**信号词**筛一遍先：

- **强信号**：判据、静默、踩过、实测、根因、之所以、以后别再、上次/以前/曾经
- **弱信号**：默认值、看起来、表面上、没有报错、照样

强信号的条目**全文带上**（教训多半在那里），弱信号的只带首行（够用来判断要不要展开）。
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

#: 教训多半出现在含这些词的提交正文里 —— 先筛一遍，别把几十行正文全倒进上下文
STRONG = ("判据", "静默", "踩过", "实测", "根因", "之所以", "别再", "以前", "曾经", "教训")
WEAK = ("默认值", "看起来", "表面上", "没有报错", "照样", "注意", "陷阱")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return r.stdout


def _commits(repo: Path, since: str, limit: int) -> list[dict]:
    """取提交：`subject` + `body` 分开拿，方便按信号词分级。"""
    raw = _git(repo, "log", f"--since={since}", f"--max-count={limit}", "--format=%x00%h%x1f%s%x1f%b")
    out = []
    for chunk in raw.split("\x00"):
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 3:
            continue
        out.append({"sha": parts[0].strip(), "subject": parts[1].strip(), "body": parts[2].strip()})
    return out


def _grade(c: dict) -> str:
    text = c["subject"] + c["body"]
    if any(k in text for k in STRONG):
        return "strong"
    if any(k in text for k in WEAK):
        return "weak"
    return "noise"


def _new_changelogs(repo: Path, since: str, limit: int) -> list[str]:
    """这段时间**新增**的 changelog 文件（排除改动的 —— 只看新增的）。"""
    raw = _git(
        repo, "log", f"--since={since}", f"--max-count={limit}",
        "--diff-filter=A", "--name-only", "--format=", "--", "changelogs/",
    )
    return sorted({l.strip() for l in raw.splitlines() if l.strip() and l.endswith(".md")})


# ----------------------------------------------------------------------
# `--check`：给 SessionStart hook 用的**静默阈值检查**
# ----------------------------------------------------------------------
#
# ⚠️ **默认必须静默**。这是这个模式存在的全部理由：hook 每次开会话都跑，
# 一旦"有信号就提示"，人两周内就会开始忽略它 —— 机制随即死掉。
# 所以判据不是"这次有没有信号"，而是"**攒够了没有**"。
#
# 为什么判据是「强信号条数」而不是「时间」或「提交数」：
#   时间与"有没有东西可收"无关（闲的两天白跑、忙的两天该收）；
#   提交数 ≠ 教训量（一次"清 lint"可能 10 个提交 0 条教训）。
# 而数强信号是**纯 git 扫描、零 LLM 成本** —— 所以这件事可以免费地每次问一遍。

#: 攒够多少条强信号就提示。少于这个数不值得打断人。
THRESHOLD_SIGNALS = 5
#: 兜底天数：一直没攒够 5 条也不能永远不收（但仍要求至少有 1 条可收）。
THRESHOLD_DAYS = 14


def _state_path(repo: Path) -> Path:
    """状态文件（**每台机器一份**，已 gitignore）。

    它记「上次回收是在哪个 commit / 什么时候」—— 于是"攒了多少" = `sha..HEAD`。
    放在 `.claude/` 下是因为 hook 也住那里；但**它不进 git**：回收是"我做过没有"，
    不是仓库的属性。
    """
    return repo / ".claude" / ".retro-state.json"


def _read_state(repo: Path) -> dict | None:
    p = _state_path(repo)
    if not p.exists():
        return None
    try:
        import json

        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 坏了就当没有，重新初始化 —— 不要因此把 hook 弄挂


def _write_state(repo: Path, sha: str) -> None:
    import json
    from datetime import UTC, datetime

    p = _state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"at": datetime.now(UTC).isoformat(), "sha": sha}, ensure_ascii=False),
        encoding="utf-8",
    )


def _signals_since(repo: Path, sha: str, limit: int) -> int:
    """自 `sha` 以来**强信号**提交的条数（不含 `sha` 本身）。"""
    raw = _git(repo, "log", f"{sha}..HEAD", f"--max-count={limit}", "--format=%x00%h%x1f%s%x1f%b")
    n = 0
    for chunk in raw.split("\x00"):
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f")
        if len(parts) >= 3 and _grade({"subject": parts[1], "body": parts[2]}) == "strong":
            n += 1
    return n


def check(repo: Path, limit: int) -> int:
    """静默阈值检查。**超阈值才打印**（hook 把 stdout 注入上下文；不打就是不打）。

    三种情况：
    - **没有状态文件** → 静默初始化（记当前 HEAD）。新克隆的人不该被催收历史的账。
    - 攒够 `THRESHOLD_SIGNALS` 条 → 提示。
    - 距上次 ≥ `THRESHOLD_DAYS` 天**且**至少 1 条可收 → 提示（兜底）。
    其余一律静默。
    """
    from datetime import UTC, datetime

    head = _git(repo, "rev-parse", "HEAD").strip()
    if not head:
        return 0  # 不是 git 仓 / 无提交 —— 静默

    state = _read_state(repo)
    if state is None:
        _write_state(repo, head)
        return 0

    sha = str(state.get("sha") or head)
    n = _signals_since(repo, sha, limit)

    days = 0
    try:
        at = datetime.fromisoformat(str(state.get("at")))
        # `max(0, …)`：状态里的时间戳可能是**未来**的（时钟偏差 / 手工造的），
        # 不夹住会打出「-1 天前收的」这种显然不自洽的提示 —— 而一条**看起来就不对**的
        # 提示，会让人连它的内容一起不信。
        days = max(0, (datetime.now(UTC) - at).days)
    except (TypeError, ValueError):
        pass

    if n >= THRESHOLD_SIGNALS:
        reason = f"自上次回收以来有 {n} 条强信号"
    elif days >= THRESHOLD_DAYS and n >= 1:
        reason = f"距上次回收已 {days} 天，期间有 {n} 条强信号"
    else:
        return 0  # ★ 静默是默认 —— 见 check() 的说明

    print(
        f"[经验回收] {reason}。跑 `make retro-harvest` 收一遍 ——\n"
        "  它只收集素材，三分类由你决定；收完会自动更新上面的计数。"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="回收经验素材，交给 LLM 三分类")
    ap.add_argument("--since", default="2 weeks ago", help='git --since 语法，如 "2 weeks ago"')
    ap.add_argument("--repo", action="append", default=None,
                    help="要扫的仓库路径（可重复；默认只扫本仓）")
    ap.add_argument("--limit", type=int, default=200, help="每个仓最多看多少条提交")
    ap.add_argument("--out", default="", help="素材输出路径（默认 /tmp/retro-harvest-<日期>.md）")
    ap.add_argument(
        "--check", action="store_true",
        help="静默阈值检查（给 SessionStart hook 用）：**超阈值才打印**，不打就是不打",
    )
    args = ap.parse_args()

    backend = Path(__file__).resolve().parent.parent
    repos = [Path(p).resolve() for p in (args.repo or [str(backend)])]
    for r in repos:
        if not (r / ".git").exists():
            raise SystemExit(f"✗ {r} 不是 git 仓库")

    if args.check:
        return check(repos[0], args.limit)

    lines: list[str] = ["# 经验回收素材\n"]
    stats: list[str] = []
    counts = {"strong": 0, "weak": 0, "noise": 0}

    for repo in repos:
        commits = _commits(repo, args.since, args.limit)
        logs = _new_changelogs(repo, args.since, args.limit)
        strong = [c for c in commits if _grade(c) == "strong"]
        weak = [c for c in commits if _grade(c) == "weak"]
        for g, n in (("strong", len(strong)), ("weak", len(weak))):
            counts[g] += n
        counts["noise"] += len(commits) - len(strong) - len(weak)
        stats.append(f"{repo.name}: {len(commits)} 条提交 / {len(logs)} 个新 changelog")

        lines.append(f"\n## 仓库 `{repo.name}`（{repo}）\n")
        if strong:
            lines.append(f"### 强信号（含「判据/静默/踩过/根因…」，**全文**）— {len(strong)} 条\n")
            for c in strong:
                lines.append(f"#### `{c['sha']}` {c['subject']}\n\n{c['body']}\n")
        if weak:
            lines.append(f"### 弱信号（只给首行，够了再展开）— {len(weak)} 条\n")
            for c in weak:
                lines.append(f"- `{c['sha']}` {c['subject']}")
            lines.append("")
        if logs:
            lines.append(f"### 新增 changelog — {len(logs)} 个\n")
            lines += [f"- {p}" for p in logs]
            lines.append("")

    out = Path(args.out) if args.out else Path(f"/tmp/retro-harvest-{__import__('datetime').date.today():%Y%m%d}.md")
    out.write_text("\n".join(lines), encoding="utf-8")

    # 记「收到这儿了」—— 阈值检查（`--check`）靠它算"自上次以来攒了多少"。
    # ⚠️ 只在**真收了**（非 --check）时写：否则一次检查就把账抹平了。
    head = _git(repos[0], "rev-parse", "HEAD").strip()
    if head:
        _write_state(repos[0], head)

    print(f"== 素材：{counts['strong']} 条强信号 / {counts['weak']} 条弱信号 / {counts['noise']} 条噪音 ==")
    for s in stats:
        print(f"   {s}")
    print(f"\n素材已写入 {out}")
    print(
        "\n下一步：把下面这段粘给 Claude Code（**三分类是它的活**，本脚本不做判断）——\n\n"
        f"    读 `docs/lessons/README.md` 的收录标准与模板，对 `{out}` 里的素材做三分类：\n"
        "    ① 可机器化（能写成「如果…就红」）→ 给出**检查该写成什么**，这是首要产出；\n"
        "    ② 只能文档 → 按模板写成条目，**没有「代价」的不收**；\n"
        "    ③ 丢弃（一次性 / 机器相关 / 已在仓库里）→ 说明为什么丢。\n"
        "    先跟 `docs/lessons/INDEX.md` 比对，**已在里面的不要重复收**（同族第二次出现，\n"
        "    恰恰说明它还没被机器化 —— 那应该升级成检查，而不是再写一遍）。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
