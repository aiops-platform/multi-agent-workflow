#!/usr/bin/env bash
#
# SessionStart hook：**攒够了才提醒**该回收经验了。
#
# ## 为什么"低于阈值一律静默"是这个脚本的全部重点
#
# hook 每次开会话都跑。一旦它"有信号就提示"，人两周内就会开始忽略它 —— 机制随即死掉。
# 所以判据不是「这次有没有信号」（本仓的提交几乎条条含"判据/实测"，必然次次命中），
# 而是「**攒够了没有**」。阈值与理由在 `scripts/retro_harvest.py` 的 `check()` 里。
#
# ## stdout 的去向（别在这上面翻车）
#
# SessionStart 的**纯文本 stdout 会被注入 Claude 的上下文**（这是该事件的既定行为，
# 不需要 JSON 信封）。但有一条**会静默吞掉输出**的规则：
#
#   stdout 若以 `{` 开头**且**以 `}` 结尾 → Claude Code 当 JSON 解析 → 解析失败就
#   **整段丢弃**，只在转录里留一个 hook error。
#
# 所以下面输出的提示语以 `[经验回收]` 开头，**永远不以 `{` 起头**。
#
# ## 失败姿态：一律静默放行
#
# 这个 hook 拦不住任何东西（SessionStart 本来就不能 block），所以它唯一能造成的伤害是
# **拖慢或打断开会话**。因此：没装 venv、脚本不在、python 报错 —— 全部 `exit 0` 且不输出。
set -euo pipefail

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
py="$root/venv/bin/python"

[ -x "$py" ] || exit 0                        # 还没 make install —— 静默
[ -f "$root/scripts/retro_harvest.py" ] || exit 0

# `|| true`：检查本身失败绝不能冒泡（set -e 下会把 hook 变成非零退出）
"$py" "$root/scripts/retro_harvest.py" --check 2>/dev/null || true

exit 0
