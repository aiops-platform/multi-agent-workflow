# TODO（待办清单）

> 目标：把 agentflow 从"单项目写死"演进成"可配置平台"。优先做高优先级项。
> 每项带「现状 / 问题 / 目标 / 涉及文件」，方便接手。

---

## 1. ⭐ Agent 配置可配置化（平台化关键）—— ✅ 主体落地 v1.12（2026-09-03）

> **落地情况（v1.12）**：DB 驱动 agent 配置（`agent_configs` 表 sqlite+PG）+ `AgentConfigResolver` 合并解析 +
> 控制面 `/agent-configs` CRUD + agent→MCP server 绑定（server 粒度）+ SIP「Agent 配置」页。
> 见 `docs/AGENT_CONFIG_DB_zh-CN.md`。**尚未覆盖**：tools 可见性 / 超时 / 限流、按租户模型参数（下表第 4 行起）。
>
> **现状**（v1.12 前的硬编码基线）：15 个 agent 的全部配置**硬编码**在项目代码里：

| 配置 | 位置 |
|---|---|
| 名字 / 角色 / 描述 | `agentflow/agents/registry.py`（`DIAGNOSE_AGENTS` / `FIX_AGENTS` 列表 + `AGENT_DESCRIPTIONS`） |
| 提示词 / 输出契约 | `agentflow/agents/prompts.py`（`SYSTEM_PROMPTS` / `AGENT_SCHEMAS`） |
| 工具可见性 / 超时 / 限流 | `agentflow/agents/tools.py`（`TOOL_REGISTRY` 每个 `ToolSpec.agents` 列表） |

**问题**：做平台必须支持租户/运营侧**配置**，而非改代码发布。典型需求：

- 新增 / 停用 / 修改 agent
- 自定义 system prompt / 输出 schema
- 调整某个 agent 可见的工具（授权矩阵）
- 按租户覆盖模型参数（model / max_iters / 是否真实 LLM）

**目标**：仿照 `workflows/*.yaml` 的声明式模式，提供 `agents/*.yaml`（或 DB 驱动）配置，注册表**从配置构建**；`AGENT_REGISTRY` 从"import 时写死"变为"运行时装配"。

**涉及**：`agentflow/agents/registry.py`、`prompts.py`、`tools.py`、（新增）配置加载层。

---

## 2. 真实 node_runner 接入 executor —— ✅ 已完成（383b6b7 + v5.3 批C）

`agents/runner.py:AgentNodeRunner` 经 `RunService(node_runner=...)` 注入 executor/API；有 `DEEPSEEK_API_KEY` 即真实 LLM（无 Key 回退 mock）。v5.3 批 C 进一步：按 `current_tenant` 路由 per-tenant MCP / agent 配置；`AGENTFLOW_SHARED_DATASOURCES=0`（默认加固）时不注入内置共享数据源工具。剩余：L2 沙箱工具接入真实 run。

## 3. `/agents` 端点增强

- 可选 `?role=diagnose|fix` 过滤
- 工具返回元数据（`level` L1/L2、`needs_approval`）而非只有工具名

---

## 4. 清预置 lint / 测试债

- `make lint` 目前有 ~37 个预置 ruff 错误（改动前后不变，非本次引入）
- ~~`tests/test_workspace.py` 引用不存在的 `agentflow.workspace`（M3 模块未落树）~~
  → **已查明并修复**（commit `67c9549`）：不是"模块未落树"，而是 `.gitignore` 里裸写的
  `workspace/` 匹配了任意深度同名目录，把源码包 `agentflow/workspace/` 整个吞掉、从未入库
- `tests/test_sandbox.py` 5 个用例依赖本机 `~/.kube/config`（本机有 kube 时会因
  incluster 配置缺失而失败；CI 无 kube 时被 skip）

---

## 5. ⭐ 评估 AgentScope 2.0.3 → 2.0.8 升级

> 2026-09-11 记录。**不是**为了追新，而是因为落后版本已经卡住了两处设计空间。

### 现状

- 项目锁定 **2.0.3**（`pyproject.toml`，CLAUDE.md 约束 1）。
- 上游最新 **2.0.8**（2026-09-08），**落后 5 个版本**。

### 为什么值得评估（两处卡点）

**① MCP 连接生命周期**：我们实测发现 stateful MCP 连接**跨 task 关闭会失败**
（`Attempted to exit cancel scope in a different task than it was entered in`
—— anyio TaskGroup 绑定创建它的 task）。而 DAGExecutor 的并行波是独立 task，
`revalidate()` 也在节点 task 内 → 一旦开 `is_stateful=true`，连接就关不掉、泄漏资源。
（同一个坎也让 **stdio MCP 的子进程泄漏**——stdio 强制 stateful。）

上游 2.0.7/2.0.8 各有一个可能相关的修复，**需确认是否解决了这个问题**：
- `fix(mcp) allow reconnecting stateful clients`（#2308，2.0.7）
- `fix(mcp) cleanup of cancelled MCP connections`（#2499，2.0.8）

**② 连接池化**：若 #2499 真解决了 task-affinity，则自建连接池可能**没必要**
（可降级为"升级 + 开 stateful"）。另注意上游 PR #1951 已在 **workspace 层**做了
`max_live_stateful_mcps`（默认 40）+ LRU 回收——说明上游认可 stateful 需要**有界管理**，
但那是在 workspace 层，不是裸 `MCPClient`。

### 已知会受影响的本仓补丁

- `agents/mcp_tool_cache.py`（`CachingMCPClient`）：读上游 `PrivateAttr` `_cached_tools`。
  升级后须复查上游是否已自行缓存列举（若已修，本补丁可删）。
  > 注：这不是"上游 bug"——其 docstring 说明该缓存是为 `get_tool` 反查被过滤的工具名，
  > 不是为省网络调用。我们打补丁是**本地取舍**（实测省 80% 会话）。

### 升级成本与验收

- **CLAUDE.md 约束 1：升级前必须重跑 S-001 / S-011 冒烟**（锁 2.0.3 的原因就是这两个）
- streaming 事件 API 可能变化（约束 1 原文）
- 回归：`make test` 全绿；testbed 两场景 E2E 复跑
- 收益确认：MCP 会话数、stateful 跨 task 关闭、stdio 子进程回收 三项前后对比

### 涉及文件

`pyproject.toml`（版本 pin）、`agents/scopes.py`（AgentScope 适配层）、
`agents/mcp_manager.py`、`agents/mcp_tool_cache.py`（可能可删）、
`agents/transcript.py`（streaming 事件）
