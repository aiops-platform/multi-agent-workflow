#!/usr/bin/env bash
#
# PreToolUse hook：**push 前先 fetch，落后 upstream 就拦下来**。
#
# ## 为什么需要它
#
# `CLAUDE.md` 的「Git 约定」里已经写着「**push 前再 `git fetch` 看一次**」——
# 但那是一行**约定**，而约定拦不住任何一次 push。本仓实测踩过两次：
# 一次 push 被拒才发现落后；**另一次更贵 —— 方案的前提已经被同事的提交作废了**，
# 而我是在 push 时才发现的。
#
# retro（2026-09-22 §4.0）的原话：「一条约束如果**能被机器检查**，就不该只写在文档里。」
# 这个脚本就是那句话的兑现。
#
# ## 退出码契约（**实测确认，不是照抄文档**）
#
#   exit 0 → 放行（stdout/stderr **都不会显示**）
#   exit 2 → 阻断，**stderr 喂回给模型**
#   exit 1 → **不阻断**！只把 stderr 给用户看，然后放行
#
# ⚠️ 最后一条是最危险的：本仓的其它脚本惯用 `set -e`，一旦某条命令非零退出
# 就会以 1 收尾 —— 那等于**这个门禁从来没生效过，而且没有任何提示**。
# 所以本脚本**刻意不用 `set -e`**，并且所有"查不出来"的分支都显式 `exit 0`
# （fetch 失败 / 不是 git 仓 / 没有 upstream），最后只有一个地方 `exit 2`。
#
# ## 为什么不用 hook 的 `if: "Bash(git push:*)"`
#
# `if` 只在**命令首段**匹配，`cd repo && git push` 这种会**漏掉** ——
# 漏掉就是 fail-open。所以这里 `matcher` 只写 `Bash`，命令匹配在脚本内做。
# 代价是每次 Bash 调用多跑一次 jq（亚毫秒），换"不漏"。

set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")

# 只在 push 时介入。
#
# ⚠️ 这是**子串匹配**，不是 shell 解析 —— 所以 `echo "git push"` 也会命中。
# 这是刻意的取舍：精确判断"这条命令到底会不会真的 push"要写一个 shell 解析器，
# 而误判的代价是**不对称**的 ——
#   · 误报（把 echo 当 push）：多跑一次 fetch，慢几百毫秒
#   · 漏报（把真 push 当 echo）：门禁静默失效，正是要防的那件事
# 按 retro 的判错方向原则（§3 第 4 条：宁可响亮地红，不要静默地漏），选误报。
case "$cmd" in
  *"git push"*) ;;
  *) exit 0 ;;
esac

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$root" 2>/dev/null || exit 0

# 不是 git 仓 → 不管
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# 没有 upstream（新分支首次 push）→ 谈不上"落后"，不管
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || exit 0
[ -n "$upstream" ] || exit 0

# fetch：**失败不拦**。离线 / 远端不可达时拦住 push 是帮倒忙。
git fetch --quiet --no-tags 2>/dev/null || exit 0

# ⚠️ 方向极易写反：`git merge-base --is-ancestor A B` 判的是「A 是 B 的祖先」。
#     · `--is-ancestor @{u} HEAD` 为真 ⇒ 远端全部提交本地都有 ⇒ **没落后**，放行
#     · 反过来写（`HEAD @{u}`）会变成"落后时反而放行"—— 那个门禁**永远不生效
#       且毫无提示**。本脚本的单元测试正是抓住这个方向的（2026-09-23）。
if git merge-base --is-ancestor '@{u}' HEAD 2>/dev/null; then
  exit 0
fi

behind=$(git rev-list --count "HEAD..@{u}" 2>/dev/null || echo "?")
ahead=$(git rev-list --count "@{u}..HEAD" 2>/dev/null || echo "?")

cat >&2 <<EOF
拦截：本地落后远端 $upstream $behind 个提交（本地另有 $ahead 个未推送）。

先 \`git fetch\` 再 rebase/merge，然后重新 push。

为什么拦：本仓是多人**并行直提 main**，远端在你干活期间动过是常态。
实测代价有两次 —— 一次 push 被拒才发现落后；另一次是**方案的前提已经被
同事的提交作废**，而发现得太晚。先看一眼的成本远低于返工。
EOF

exit 2
