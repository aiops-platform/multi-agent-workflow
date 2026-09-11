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

---

## 6. ⭐⭐ CMDB 生产化（**当前最关键的生产阻塞**）

> 2026-09-11 审计发现。**生产路径依赖一个 mock，且硬编码了个人绝对路径，还违反多租户隔离。**

### 现状（三处叠加）

**① 硬编码个人路径** —— `workspace/prepare.py:27`

```python
DEFAULT_REPO_ROOT = Path("/Users/bo.gong/Documents/accenture/workspace/agentflow-testbed/services")
```

**② 走的是 mock，且在 API/Worker 的生产装配里** —— `api/app.py:251` `build_cmdb()`
→ `MockCmdbProvider(default_cmdb())`，被 `app.py:315` 与 `worker.py:350` 注入
`AgentNodeRunner`。`default_cmdb()` 只映射 `local` / `team-alpha` **两个租户**。

**③ 接口本身不支持多租户** —— `workspace/cmdb.py:31`

```python
async def get_repo_for_service(self, service: str) -> RepoSpec | None:
    for services in self._mapping.values():     # ← 遍历所有租户的映射
        if service in services: ...
```

`TenantMappingProvider.get_repo_for_service(service)` **签名里没有 tenant_id**，
无法做租户隔离；实现遍历全部租户 → **跨租户返回同一个 repo**（违反 v5.3 P1）。

### 影响

- 换台机器 / 换个人跑 → 路径不存在 → 工作区准备全失败 → 修复侧 agent 拿不到工作区
- 非 `local`/`team-alpha` 的租户 → CMDB 全空 → `locate_code` 返回 `found=false`
  → code-locator 定位不到仓库

### 目标

1. **接口加 tenant**：`get_repo_for_service(tenant_id, service)`（调用方与两处实现同步改）
2. **repo 根配置化**：`AGENTFLOW_REPO_ROOT`（或直接由 CMDB 给出绝对 URL），去掉个人路径
3. **真实 CMDB 适配器**：接 v5.3 §9.4 的 `TenantMappingProvider` 生产实现（CMDB + 拓扑查询）
4. 或（v5.3 P1 方向）改为**由租户 MCP 提供 repo 映射**，与数据面统一

### 涉及文件

`workspace/cmdb.py`（接口 + Mock 实现）、`workspace/prepare.py`（default_cmdb + 路径）、
`api/app.py:build_cmdb`、`worker.py`、`agents/tools.py`（`_cmdb_locate_code`）

---

## 7. ⭐⭐ 审批通知渠道（human-in-the-loop 断链）

> `approval/notifier.py:3` 自述：「本地 MVP：日志通知（通知渠道为占位接口，**M6 接邮件/Slack/webhook**）」——
> **M6 已交付，但这部分没做**。

### 现状

`ApprovalNotifier.notify()` 只 `log.info(...)` 并返回记录，**不推送给任何人**。

### 影响

审批挂起时**审批人不会收到任何通知**，只能靠人盯 UI 或等 sweeper 超时自动拒绝。
在 v5.4「审批是稀缺资源、human-in-the-loop」的设计里这是硬伤——它让"人会及时批"
这个前提不成立，实际会退化成"审批必然超时"。

### 目标

- 通知渠道适配器（邮件 / Slack / webhook，至少一种）
- 通知模板（含 run/node/租户、审批链接、超时时刻）
- 重试与失败降级（通知失败不能阻断审批流）
- 与 sweeper 联动：临近超时要不要二次提醒

### 涉及文件

`approval/notifier.py`、`config.py`（渠道配置）、`approval/sweeper.py`（超时前提醒）

---

## 8. MCP server 部署与安全

`aiops-mcp-servers/servers/aiops-datasource-mcp-server/`

| 项 | 现状 | 目标 |
|---|---|---|
| **Dockerfile** | ❌ 无（同仓 `git-mcp-server` 有，可对照） | 多阶段构建 + 非 root + HEALTHCHECK + 镜像内装 kubectl |
| **认证** | `AUTH_TOKEN=` 为空（dev） | 生产设 `ENVIRONMENT=production` + 非空 token |
| **凭证明文** | 若启用 token，`mcp_servers.config.headers` 在 agentflow 库**明文存储 + GET 回显** | 加密列 + 回显脱敏（`cryptography` 已在依赖里） |
| **kubectl** | 进程需 kubectl + kubeconfig | 镜像内置 + ServiceAccount 挂载 |

---

## 9. MCP 连接池化（设计已定，前置见第 5 项）

**收益**：执行会话 1/调用 → 0；**修好 stdio 子进程泄漏**（既存缺陷——stdio 强制
stateful，evict 时跨 task `close()` 失败只打警告，子进程真泄漏）。

**已用原型验证可行**（2026-09-11）：owner task 独占持有连接，enter/use/exit 都在其内
→ 消除 anyio 跨 task 取消域问题。实测任意 task 借用、5 路并发复用、跨 task 关闭**全部通过**。

**前置**：先做第 5 项（AgentScope 升级评估）——若上游 2.0.8 的
`fix(mcp) cleanup of cancelled MCP connections`（#2499）已解决 task-affinity，
本项可降级为「升级 + 开 stateful」，不必自建池。

**设计要点**：池键必须含 tenant（否则跨租户复用）；借用超时；故障重建；优雅关闭顺序。

**涉及**：新增池模块 + `agents/mcp_manager.py`（revalidate/evict 交互）

---

## 10. 沙箱安全加固

- `sandbox/exec_service.py` 只有**路径白名单**，**无请求认证**（谁能连上就能执行代码）
- 无 egress 控制（沙箱可外联）
- 目标：exec 服务加 token 校验 + NetworkPolicy 限定出口

---

## 11. 其余小项

| 项 | 说明 |
|---|---|
| 端口约定同步 | `8300` 等端口在 config.py / .env.example / README / 本地 .env 四处需人工同步，易漂移 |
| agent 无谓调用 | 实测 `query_metrics` 被调 20 次（5 个指标各一次即够）；`check_infra(namespace="default")` 传错 namespace 而返回 0 pod。prompt 已加约束，属模型行为，需持续观察 |
| `search_knowledge` 真后端 | 恒返回 `INC0001`（**每次诊断都"命中"同一条假事故**）。按 design-v5.4 §7.3 走租户 MCP，等接缝 |
| `get_trace` 启发式环境依赖 | 词表是测试床经验，换环境可能失效（design-v5.5 §8.1 已声明为设计边界） |

---

## 12. 已完成（留痕）

- ~~Worker 不热载 agent 配置~~ → ✅ `911c7d3`：按库内指纹 TTL 热载，绑定 MCP server 无需重启
- ~~本地直连数据源实现~~ → ✅ `578ea40`（批 3）：`datasources.py` 及脚本删除，取数 MCP-only
- ~~MCP 工具列举重复握手~~ → ✅ `b8e1c88`：TTL 记忆化，单次节点执行会话 56 → 13（-80%）
- ~~`agentflow.workspace` 未入库~~ → ✅ `67c9549`：`.gitignore` 裸 `workspace/` 吞源码
- ~~审批超时 Resume 卡死 / 幂等未接线 / JWT 多租户~~ → ✅ `bb7b8ce` / `383b6b7`
