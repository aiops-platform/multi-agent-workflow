#!/usr/bin/env bash
#
# PreToolUse hook：**提交的"爆炸半径"过大时，说一句**（不阻断）。
#
# ## 为什么是"出声"而不是"拦"
#
# retro 的硬规矩第 2 条是「**爆炸半径** —— 一个提交 ≤ 1 个主题」。
# 但"是不是一个主题"只有写代码的人知道，机器判不了 ——
# 本仓一天 20 条提交跨 5 个主题那次，每条单独看都合理，问题出在**没有停顿点**。
#
# 所以这个 hook 的目标不是拦住，而是**制造一个停顿点**：把"这次改动有多大、
# 碰到了哪几个区域"摆到眼前，让人自己判断要不要拆。
# 拦下来会误伤合法的重构，出声不会。
#
# ## 输出通道（**实测确认**）
#
# PreToolUse 里 **`exit 0` + 普通 stdout 什么都不显示** —— 模型、用户、转录都看不到，
# 是个静默空操作。要不阻断又被看见，只能走 JSON：
#
#   systemMessage                              → **用户**看到
#   hookSpecificOutput.additionalContext       → **模型**看到（实测：模型能逐字复述）
#
# 两个都给，是因为两者受众不同：人需要看到"这次改动很大"，模型需要据此
# 在提交信息里如实描述改动面、或者建议拆分。
#
# 实测记录（2026-09-23）：
#   · 纯 echo + exit 0  → 模型答「都没有出现」
#   · 上面的 JSON       → 模型答「WARN-CTX 出现了（在 PreToolUse 附加上下文里）」
#
# ## 退出码
#
# **恒为 0**。这个 hook 不该阻断任何东西；所有失败路径（不是 git 仓、
# 没有暂存改动、jq 缺失）也一律 exit 0 —— 一个提醒器把自己搞挂是最糟的。

set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")

case "$cmd" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$root" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# 阈值取自本仓近期 40 个提交的分布（中位 4 文件/156 行；p90 12 文件/854 行）。
# 取 ≈p90 ⇒ 约 10% 的提交会出声 —— 太频繁会被忽略，太稀疏则等于没有。
MAX_FILES=15
MAX_LINES=800

numstat=$(git diff --cached --numstat 2>/dev/null) || exit 0
[ -n "$numstat" ] || exit 0

files=$(printf '%s\n' "$numstat" | grep -c . )
lines=$(printf '%s\n' "$numstat" | awk '{ if ($1 != "-") s += $1 + $2 } END { print s+0 }')

# 改动碰到了几个"区域"：比总行数更接近"跨了几个主题"。
areas=$(printf '%s\n' "$numstat" | awk '{print $3}' \
        | awk -F/ '{ if (NF>=3) print $1"/"$2; else print $1 }' \
        | sort -u | paste -sd, -)

if [ "$files" -le "$MAX_FILES" ] && [ "$lines" -le "$MAX_LINES" ]; then
  exit 0
fi

msg="改动面提醒：本次暂存 $files 个文件 / $lines 行，跨区域：${areas}。
retro 的硬规矩是「一个提交 ≤ 1 个主题」—— 如果这几块不是同一件事，考虑拆开提交。"

# ⚠️ stdout 只允许输出这一段 JSON，不能有别的任何字符（否则解析失败、整段被丢弃）。
jq -n --arg m "$msg" '{
  systemMessage: $m,
  hookSpecificOutput: { hookEventName: "PreToolUse", additionalContext: $m }
}' 2>/dev/null || exit 0

exit 0
