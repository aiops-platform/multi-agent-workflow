#!/usr/bin/env bash
#
# SessionStart hook：**让代码索引自己保持最新**（后台跑，全程静默）。
#
# ## 为什么"维护"必须自动化
#
# 索引的价值完全取决于它新不新 —— 一份过期的图**比没有图更糟**，因为它给出
# 确信的错误答案（本会话实测过：无 daemon 时 `codegraph query` 对刚写的符号
# 返回 `No results found`，且只有 `codegraph status` 会提示，`query` 不会）。
#
# codegraph 自带文件监听，但**它只在 MCP server 活着时生效** ——
# 也就是"agent 会话期间"。而索引恰恰在会话**之外**被改坏：人在编辑器里改代码
# （没有 agent 在跑），监听不工作，索引就停在那儿。
#
# 所以每次开会话同步一次，正好补上那个缺口：
#   · 会话内    → MCP 的文件监听实时跟（工具自带，已实测）
#   · 会话之间  → 这个 hook 在下次开会话时补齐（实测 `Caught up N file(s) changed`）
#
# ## 三条刻意的设计
#
# 1. **后台跑**（`nohup ... &`）：同步要起 npx、读盘，不能拖慢开会话。
# 2. **全程静默**：这是维护动作，不是通知。有输出反而会变成噪音
#    （对比同目录的 `retro-harvest-check.sh` —— 那个**只在攒够时才出声**，
#     两者姿态一致：默认不打扰）。
# 3. **没装就退**：`.codegraph/` 不存在（队友没采用）→ 直接 exit 0，
#    绝不因为"这个可选的开发工具"给任何人添麻烦。
#
# ⚠️ 与 §9.6 的沙箱约束无关：这个 hook 在**宿主**上跑，不在沙箱里。
#    但它执行的确实是第三方代码（npx 拉的那 289MB 自包含二进制）——
#    这也是为什么它**只在已 init 的仓库里**才跑，而不是无条件拉起。

set -uo pipefail

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$root" 2>/dev/null || exit 0

# 没采用 codegraph 的仓库 → 静默退出（这是可选的开发工具，不是系统依赖）
[ -d .codegraph ] || exit 0

# 没有 node / npx → 静默退出（`make doctor` 的 toolchain 那项负责提醒这件事）
command -v npx >/dev/null 2>&1 || exit 0

# 后台同步：`nohup` + 重定向，确保 hook 退出后子进程不被带走、也不污染输出。
# 版本与 Makefile 的 `make codegraph` 钉的是同一个 —— 两处不一致会导致
# 同一台机器上有两个版本的索引器交替写同一个 db。
nohup npx -y @colbymchenry/codegraph@1.6.0 sync . >/dev/null 2>&1 &

exit 0
