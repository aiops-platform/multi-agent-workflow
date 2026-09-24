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

## 2. codegraph：团队怎么共用

### 2.1 索引**不进 git**，约定进 git

codegraph 自带的 `.codegraph/.gitignore` 注释原文：

> CodeGraph data files — **local to each machine, not for committing**.

这不是洁癖，是**必然**：索引必须与代码同步，而**入库的索引对任何改过代码的人
立刻就过期** —— 一份过期的图**比没有图更糟**，它给出确信的错误答案。

所以：

| | 进 git？ | 为什么 |
|---|---|---|
| `.codegraph/.gitignore` | ✅ | 让每台机器的索引目录干净，不会在 `git status` 里碍眼 |
| `codegraph.db`（8 MB） | ❌ | 每台机器各自建，几秒的事 |
| `codegraph.json`（若将来加自定义 exclude / 语言映射） | ✅ | README 明说"commit the file to share the mapping with your team" |

### 2.2 上手：一条命令

```bash
make codegraph          # 没索引就建，有就增量同步 + 报状态
make codegraph MCP=1    # 顺带把 MCP server 接进 agent（**一次性**，改本机 ~/.claude.json）
```

`MCP=1` 之后，**会话期间索引自动跟**（工具的文件监听，实测：改文件 6 秒后查询即命中）。
不接 MCP 也能用，只是得手动 `make codegraph`。

### 2.3 维护：三层，各管一段

这是"索引会不会过期"的完整答案（逐层实测过）：

| 层 | 什么时候生效 | 谁负责 |
|---|---|---|
| **① 文件监听自动同步** | **agent 会话期间**（`codegraph serve --mcp` 拉起 daemon 后） | 工具自带。防抖 2000ms（`CODEGRAPH_WATCH_DEBOUNCE_MS` 可调） |
| **② 会话之间补齐** | 每次**开新会话**时 | `.claude/hooks/codegraph-sync.sh`（SessionStart，**后台跑、0.03 秒返回、全程静默**） |
| **③ 手动** | 随便什么时候 | `make codegraph` |

**为什么必须有 ②**：索引恰恰在会话**之外**被改坏 —— 人在编辑器里改代码，
而那时没有 agent 在跑，文件监听不工作。实测 daemon 缺位期间改的文件，
重启后 daemon 自己 `Caught up 1 file(s) changed since last run`；
② 就是把这个补齐动作提前到开会话时。

### 2.4 ⚠️ 一个会静默咬人的坑

**纯 CLI 用法下，索引过期时 `query` 会静默返回过期结果。**

实测：无 daemon 时对一个刚写的符号跑 `codegraph query` → `No results found`，
**而文件明明存在**。分两种情况：

- `codegraph status` **会警告**（`Pending Changes: Added: 1 files`）
- `query` / `node` / `callers` / `callees` **不警告**，直接读库

所以：**用 CLI 时，若不确定索引新不新，先跑 `make codegraph`。**
（接 MCP 后这层由工具的 Pending 横幅覆盖 —— 但那条我没能构造出触发时序，**未验证**。）

---

## 3. 改这个目录时注意

- 本目录的东西**不该出现在关键路径上** —— 它们是可选的开发工具。
  `make doctor` 的 toolchain 那项只把 **`jq`** 列为必需（`.claude/hooks/` 的三个
  PreToolUse 检查全靠它解析 stdin，缺了会静默失效）；**node 是可选项**，
  因为只有用索引工具时才需要。
- 新增工具约定时，先回答 retro §7.1 那个问题：**谁在什么时刻会读它？**
  答不出来就别写。
