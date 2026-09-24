# 开发工具（团队共享的约定）

> **受众**：人（新队友上手）+ LLM（按需查"这个工具怎么用"）。
> **什么时候读**：想给代码加一个"能被查询"的索引时；或想改本仓用的开发工具时。

本目录放**工具面的约定**，不放代码文档（那些在 `docs/constraints/`）。

---

## 1. 三种"读懂代码"的方式 —— 实测对比

→ [`code-intel-comparison.html`](code-intel-comparison.html)（自包含，浏览器直接打开）

以 `aiops-mcp-servers` 为靶子的实测结论（2026-09-23）：

| 方式 | 答对 | 成本 |
|---|---|---|
| grep（基线） | 8/8 | 5 次调用，但每次都要现搜 |
| **understand-anything** | 6/8 | **824,147 token / 14 子代理 / 16.4 分钟** |
| **codegraph** | **7/8** | **0 token / 4.66 秒** |

一句话：**codegraph 常态用；understand-anything 只在"给新人出架构导览"时跑一次**；
grep 不可替代（字面常量、状态核查、跑服务）。

报告里也写了它**给不了**什么（无语义摘要、无架构分层）与**两家共同的盲区**
（嵌套闭包不是一等符号）—— 用之前请连同 §7「局限」一起读。

---

## 2. codegraph：团队怎么用

### 2.1 一句话概括

> **索引不进 git，约定进 git；装了的人自动接上，没装的人不受影响。**

### 2.2 三道门 —— 先说清楚哪些能自动化、哪些不能

新队友 clone 之后，要走到"Claude Code 自动用 codegraph 找代码"，中间有三道门：

| # | 门 | 能靠提交文件自动过吗 | 说明 |
|---|---|---|---|
| ① | **发现 MCP server** | ✅ **能** | `.mcp.json` 在仓库根，官方文档：*"Check `.mcp.json` into version control so everyone on your team gets the same MCP tools and services."* |
| ② | **工作区信任** | ❌ **不能，必须人手点一次** | 官方原文：*"**A cloned repository can't approve its own servers**"* —— 仓库里的 `enableAllProjectMcpServers` 在未信任的工作区里**被忽略**。这是刻意的安全设计 |
| ③ | **建索引** | ❌ **不能** | 工具明说 *"indexing is the user's decision"* —— 它不会替你建。得有一个人/一条命令来做 |

**所以"clone 后零操作"在架构上不可达。** 实际流程是：

```bash
# 1) clone 后在仓库目录跑一次 claude，接受工作区信任对话框（这一步省不掉）
# 2) 装 codegraph（没装的话）
codegraph install --yes --location=local --init
#    没装过 codegraph 的先用 npx 起一次：
#    npx @colbymchenry/codegraph install --yes --location=local --init

# 3) 此后开会话，Claude Code 自动接上 MCP，Claude 也会优先用它（见 2.4）
```

`make doctor` 会告诉你缺哪一步（见 2.5）。

> ⚠️ **未信任时还有第二个静默失效**：项目 `.claude/settings.json` 里的
> `permissions.allow` 也会被丢弃，实测 stderr 原文：
> *"Ignoring 1 permissions.allow entry … this workspace has not been trusted."*

### 2.3 提交了什么、没提交什么

| 文件 | 进 git | 作用 |
|---|---|---|
| `.mcp.json` | ✅ | **定义** server（`command` / `args`）。Claude Code 读它来发现 server |
| `.claude/settings.json` | ✅ | **批准**（`enableAllProjectMcpServers`）+ **权限**（`mcp__codegraph__*`）。⚠️ 它**不能定义** server |
| `.claude/CLAUDE.md` | ✅ | 那段"优先用 codegraph 而非 grep"的指令（每 session 自动加载，见 2.4） |
| `codegraph.db`（8 MB） | ❌ | 索引本身。工具明说 *"local to each machine, not for committing"* |
| `.claude/settings.local.json` | ❌ | 每个人**本机**的信任与批准记录 |

**为什么索引不入库**：它必须与代码同步，而**入库的索引对任何改过代码的人立刻就过期** ——
一份过期的图**比没有图更糟**，它给出确信的错误答案。

**`.mcp.json` 里为什么不写 `"command": "codegraph"`**（工具的 `install` 生成的就是这个）：
那要求 `codegraph` 在 **PATH** 上，而它是装在 `~/.local/bin`（该目录常常不在 PATH，
尤其从 IDE/GUI 启动时）。后果是 `Executable not found in $PATH`，**而且 `-p` 模式下完全静默**
（会话照常跑完、一个字都不提，只有 `--debug-file` 看得见）。所以这里写绝对路径 + `${HOME}` 展开。

### 2.4 Claude Code 怎么知道"优先用 codegraph"

靠 `.claude/CLAUDE.md` 里那段由 `codegraph install` 生成的指令 ——
**`CLAUDE.md` 每 session 全量加载**，所以它是真的每次都生效，不是靠人记得翻文档。

那段指令有三条值得学的措辞（我保留原文没改）：

- **"reach for it BEFORE grep/find or reading files"** —— 明确的优先级
- **MCP 不可用时走 `codegraph explore`** —— 有降级路径
- **"If there is no `.codegraph/` directory, skip CodeGraph entirely"** —— 没索引就整个跳过，
  **不硬推、不报错**（所以没装的人不受任何影响）

### 2.5 维护：索引靠什么保持最新

**不需要你手动维护。** 两层，都是自动的：

| 层 | 什么时候 | 谁做 |
|---|---|---|
| **① 会话期间实时同步** | agent 会话进行中 | 工具自带的文件监听（防抖 2000ms） |
| **② 会话之间补齐** | 每次**开新会话**时 | MCP server 启动，daemon 自己 `Caught up N file(s) changed since last run` |

两层都实测过。②尤其关键 —— 索引恰恰在会话**之外**被改坏（人在编辑器里改代码时
没有 agent 在跑），而 daemon 下次启动时会自己补齐。

> 曾经这里有过第三层：一个 SessionStart hook 手动跑 `sync`。
> **已删除** —— 它与 ② 完全重叠，属于"看着有用、实际不生效"的机制。

**`make codegraph` 仍保留**，但它管的是另外两件事，不是日常维护：

- **新队友第一次建索引**（第 ③ 道门）
- **排查**：`codegraph status` 能一眼看出索引新不新

### 2.6 ⚠️ 一个会静默咬人的坑

**纯 CLI 用法下，索引过期时 `query` 会静默返回过期结果。**

实测：无 daemon 时对一个刚写的符号跑 `codegraph query` → `No results found`，
**而文件明明存在**。分两种情况：

- `codegraph status` **会警告**（`Pending Changes: Added: 1 files`）
- `query` / `node` / `callers` / `callees` **不警告**，直接读库

接了 MCP（daemon 活着）时，这层由工具的 Pending 横幅覆盖 —— 但那条**我没能构造出
触发时序，未验证**。所以：**用 CLI 时，若不确定索引新不新，先跑 `make codegraph`。**

---

## 3. 改这个目录时注意

- 本目录的东西**不该出现在关键路径上** —— 它们是可选的开发工具。
  `make doctor` 的 toolchain 那项把 **`jq`** 列为**必需**（`.claude/hooks/` 的两个
  PreToolUse 检查全靠它解析 stdin，缺了会静默失效）；**codegraph 是 `advisory`**
  （缺了会提示、但不算问题、不影响退出码）；**node 是 `optional`**。
  三档语义见 `scripts/doctor.py:check_toolchain` 的 docstring。
- 新增工具约定时，先回答 retro §7.1 那个问题：**谁在什么时刻会读它？**
  答不出来就别写。
