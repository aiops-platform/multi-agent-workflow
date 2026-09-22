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


def main() -> int:
    ap = argparse.ArgumentParser(description="回收经验素材，交给 LLM 三分类")
    ap.add_argument("--since", default="2 weeks ago", help='git --since 语法，如 "2 weeks ago"')
    ap.add_argument("--repo", action="append", default=None,
                    help="要扫的仓库路径（可重复；默认只扫本仓）")
    ap.add_argument("--limit", type=int, default=200, help="每个仓最多看多少条提交")
    ap.add_argument("--out", default="", help="素材输出路径（默认 /tmp/retro-harvest-<日期>.md）")
    args = ap.parse_args()

    backend = Path(__file__).resolve().parent.parent
    repos = [Path(p).resolve() for p in (args.repo or [str(backend)])]
    for r in repos:
        if not (r / ".git").exists():
            raise SystemExit(f"✗ {r} 不是 git 仓库")

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
