# TODO（待办清单）

> 目标：把 agentflow 从"单项目写死"演进成"可配置平台"。
> 每项带「现状 / 问题 / 目标 / 涉及文件」，方便接手。
>
> **排序原则（降序）**：① 是否阻塞上生产 / 安全 → ② 是否卡住别的活（前置关系）→ ③ 产品价值。
> **已完成主体的项不再保留正文**，只在 §11 留一行痕迹；但仍未做完的尾巴统一收在 §7。

| # | 项 | 为什么排这个位置 |
|---|---|---|
| 1 | 审批通知渠道 | 生产阻塞。human-in-the-loop **断链**，且拖垮的是**已实施**的节点级审批 |
| 2 | MCP server 部署与安全 | 生产阻塞。无镜像/无认证/凭证明文，**上不了生产环境** |
| 3 | 沙箱安全加固 | 生产阻塞（安全）。无认证无 egress，**谁能连上就能执行代码** |
| 4 | 动态编排（编排层） | 产品主方向，但**须人工评审批准**才能开工，故不高于上面三项 |
| 5 | AgentScope 升级评估 | **前置项**：解锁 §6，并决定 stateful 能不能开 |
| 6 | MCP 连接复用 | 修既存缺陷（stdio 子进程泄漏），前置是 §5；**经查证不做完整池** |
| 7 | 已完成主体的遗留尾巴 | 真活但零散、不阻塞，容易被忘 |
| 8 | 清预置 lint / 测试债 | 不阻塞，但债会让回归信号不可信 |
| 9 | `/agents` 端点增强 | 体验项 |
| 10 | 其余小项 | 观察项 / 长期项 |
| 11 | 已完成（留痕） | 无正文，只记 commit 与结论 |

---

## 1. ⭐⭐ 审批通知渠道（human-in-the-loop 断链）

> `approval/notifier.py:3` 自述：「本地 MVP：日志通知（通知渠道为占位接口，**M6 接邮件/Slack/webhook**）」——
> **M6 已交付，但这部分没做**。

### 现状

`ApprovalNotifier.notify()` 只 `log.info(...)` 并返回记录，**不推送给任何人**。

### 影响

审批挂起时**审批人不会收到任何通知**，只能靠人盯 UI 或等 sweeper 超时自动拒绝。
在 design-v5.6 §4.6「审批是稀缺资源、human-in-the-loop」的设计里这是硬伤——它让"人会及时批"
这个前提不成立，实际会退化成"审批必然超时"。**注意**：v5.6 §4.6 属编排层（§4），
**未实施**；本项对**现有节点级审批**（已实施）同样是硬伤，故独立于 §4 先行。

### 目标

- 通知渠道适配器（邮件 / Slack / webhook，至少一种）
- 通知模板（含 run/node/租户、审批链接、超时时刻）
- 重试与失败降级（通知失败不能阻断审批流）
- 与 sweeper 联动：临近超时要不要二次提醒

### 涉及文件

`approval/notifier.py`、`config.py`（渠道配置）、`approval/sweeper.py`（超时前提醒）

---

## 2. ⭐⭐ MCP server 部署与安全

`aiops-mcp-servers/servers/aiops-datasource-mcp-server/`

| 项 | 现状 | 目标 |
|---|---|---|
| **Dockerfile** | ❌ 无（同仓 `git-mcp-server` 有，可对照） | 多阶段构建 + 非 root + HEALTHCHECK + 镜像内装 kubectl |
| **认证** | `AUTH_TOKEN=` 为空（dev） | 生产设 `ENVIRONMENT=production` + 非空 token |
| **凭证明文** | 若启用 token，`mcp_servers.config.headers` 在 agentflow 库**明文存储 + GET 回显** | 加密列 + 回显脱敏（`cryptography` 已在依赖里） |
| **kubectl** | 进程需 kubectl + kubeconfig | 镜像内置 + ServiceAccount 挂载 |

---

## 3. ⭐⭐ 沙箱安全加固

- `sandbox/exec_service.py` 只有**路径白名单**，**无请求认证**（谁能连上就能执行代码）
- 无 egress 控制（沙箱可外联）
- 目标：exec 服务加 token 校验 + NetworkPolicy 限定出口

---

## 4. ⭐⭐ 动态编排（编排层）—— **未实施**，设计见 `docs/design-v5.6.md` §4

> 2026-09-11 记录。来源：`design-v5.4.md`（已并入 `docs/design-v5.6.md`）。
> **不是缺陷，是有意未开工**：设计稿为 design-only，批次须**人工评审批准后**才实施。
> 记在此处是为了让"没做完的部分"有个可查的落点，别让 v5.6 §4 读起来像已完成。

### 现状（代码级核实，2026-09-11）

- workflow **一律人工**在 `POST /run` 指定 `workflow_id` 或 `workflow_yaml`，
  两者都缺直接 400（`api/app.py:362-393`）——**没有自然语言选路入口**；
- catalog = 3 张手写 workflow（`bug-fix-pipeline` / `bug-fix-scenario2` / `git-search-approval`），
  `workflows` 表 schema 仅 `id/name/yaml/created_at`（`api/workflow_store.py:16-21`），**无 meta 列**；
- `triage` agent 输出 `symptom_type`，语义上正是漏斗层 1 的输入信号，但**不参与任何选路**；
- 全仓无 `dispatch` / `planner` / `compiler` 模块，无 `DispatchDecisionSchema` / `PlanSpec` /
  `applicability` / `max_risk` / `workflow_ref` 任何符号。

> **同名干扰项（排查时别误判）**：`sandbox/exec_service.py:135` 的 `_dispatch` 是 HTTP
> 路由分发；`agents/registry.py` 的 `fix-planner` 是**静态图内**产出修复计划的节点 agent。

### 目标

让"选哪个 workflow / 要不要现编一张"可自适应，同时把 AI 自由度关在安全闸门内。
总原则：**规划期自由、执行期确定性**（动态产物 = 普通 workflow，照走冻结 DAG）。

### 批次拆解（设计定稿，按 A→B→C 顺序，每批独立提交）

**批 A —— 命中判定 + dispatch**（先做，是入口）

| 子项 | 内容 | 涉及文件 |
|---|---|---|
| A1 | `workflows` 表补 **meta JSON 列**（`applicability`/`status`/`max_risk`/`approval_policy`/`success_rate`/`origin`），sqlite/PG 幂等 migration | `api/workflow_store.py` |
| A2 | 新增 **`dispatch` agent** + `DispatchDecisionSchema`（注册进 `AGENT_SCHEMAS`） | `agents/schemas.py` / `prompts.py` / `registry.py` |
| A3 | **JSON-Schema 运行时校验层**（仅对 dispatch/planner 强制，失败重试/降级，不静默放行）——**这是 §4.3.1 自认的现状缺口，批 A 的前置** | 新增校验 util + `agents/scopes.py` |
| A4 | 5 层漏斗的**确定性层**（1 归一 / 2 结构化过滤 / 5 信任门槛）；层 3 语义召回可后置；层 4 接 dispatch | 新增 dispatch 模块 |
| A5 | 信任门槛命中 `draft`/超 `max_risk` → **注入 approval 节点**（降权） | 同上 |

> 批 A **不动 executor**。验收：单测覆盖"结构化过滤命中/漏判、schema 校验失败重试、
> draft 追加审批、跨租户 ref → miss"；demo 走「已知故障 → 命中 L2 → 自治 run」。

**批 B —— 动态合成 + compiler + 计划审批**

| 子项 | 内容 | 涉及文件 |
|---|---|---|
| B1 | PlanSpec IR + **compiler**（capability→node / 依赖→edges / 自动插审批 / 静态校验） | 新增 `planner/compiler.py` |
| B2 | 编译产物落库 `meta.origin=generated` + `planner_session` 溯源 | `api/workflow_store.py` |
| B3 | **计划级审批**（run 先落 `waiting_approval` 展示计划预览，复用既有 approval + CAS） | `service.py` / approve 端点 |
| B4 | 审批节点 id **稳定语义命名**约束（`approve-change`/`approve-pr`），否则 default-deny 403 | compiler |
| B5 | 规划预算（planner ≤3 迭代 / compiler ≤3 重排 / ≤30 节点 ≤10 层）超限 escalate | compiler + dispatch |

> **前置**：`docs/design-v5.6.md` §5.2 的 **4 项接缝开放问题**（capability 粒度 /
> planner 是否需预知本租户工具可用性 / 生成图窗口来源 / CMDB 工具能否规划期调用）
> 原两稿均未定，**批 B 前需评审**。
> 验收：E2E 走「未见故障 → miss → planner → compiler 落库 → 计划审批 → 执行」；
> 编译失败带错因回传重排；生成图审批节点被 default-deny 管住（403 断言）。

**批 C —— 闭环收敛 + 观测**

| 子项 | 内容 | 涉及文件 |
|---|---|---|
| C1 | **run 指标聚合**（按 `(tenant, workflow_name/meta.origin)` 维度：count / 成功率 / 平均节点数 / 审批通过率）——**§4.7.2 自认的现状缺口**，漏斗层 5 与晋升阈值都依赖它 | 新增聚合查询（需 join `workflow_snapshots`） |
| C2 | playbook **晋升流程**（L3 → 人工 review → `status=validated` + `origin=catalog`） | 新增晋升动作 |
| C3 | §4.8.3 监控项（分档占比 / 命中率 / 晋升率 / 计划审批超时数） | 观测层 |
| C4 | 成功率**回灌** `meta.success_rate`，驱动漏斗层 5 与晋升 | 同 C1 |

> 验收：晋升阈值 + 成功率回写后，同一事件二次命中走 L2 而非 L3。

### 依赖 / 前置

- **`search_knowledge` 真后端**（见 §10）：§4.2 复杂度判定的"知识命中"信号现为 mock，
  只能当弱信号；真后端走租户 MCP，**独立排期**，不阻塞批 A；
- **审批通知渠道**（见 §1）：`human-in-the-loop` 若审批人收不到通知，
  「审批必然超时」会使计划审批退化成形式。

---

## 5. ⭐ 评估 AgentScope 2.0.3 → 2.0.8 升级

> 2026-09-11 记录。**不是**为了追新，而是因为落后版本已经卡住了两处设计空间。
> **排在第 5 位是因为它是 §6 的前置**——先看上游修没修，再决定 §6 要不要自建。

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

## 6. MCP 连接复用（先验 #2499，再决定要不要自建）

> 2026-09-11 修订。原题「MCP 连接池化」按"自建池"排期；**查证上游后降级**——
> 上游的池不在我们的技术栈里，且它解决的问题与我们真正的问题不是一回事。

### 真正要解决的两件事（都还没解决）

| # | 问题 | 根因 |
|---|---|---|
| (a) | 执行会话 **1/调用** | HTTP 绑定默认 **stateless**（`api/mcp_store.py:30`、`api/app.py:526`）→ 每次工具调用新建 session |
| (b) | **stdio 子进程泄漏** / 跨 task 关闭失败 | **连接所有权**问题：stateful 连接由**节点 task** 持有，而 DAGExecutor 的并行波是独立 task → 关闭时 anyio 报 `Attempted to exit cancel scope in a different task than it was entered in` |

(a) 的解法就是设 `is_stateful=true`，但那会立刻撞上 (b)。**所以 (b) 是前置。**

### 为什么"自建连接池"被降级掉（2026-09-11 查证）

**上游的池不在 `MCPClient` 里，也不在我们的栈里。**

- `MCPClient` 仍是**一实例一 session**，无池、无跨调用复用；唯一缓存 `_cached_tools` 存的是工具描述（核过本地 2.0.3 与上游 main）。
- 池在 **`workspace/_base.py`**（上游 PR #1951）：`max_live_stateful_mcps`（默认 `max(40, 2×stateful)`）+ LRU 淘汰，**键 = `(agent_id, session_id)`**，淘汰粒度 turn 级。
- 我们**只 import 了** `agentscope.{agent,message,model,permission,middleware,state,tool,mcp}`，**没有 `agentscope.workspace`** → 那个池对我们**零作用**。
- 键控模型也不兼容：上游按 session 隔离，我们按 **tenant**（v5.3 P4 物理不可见），且 client **跨 agent 共享**（一个 server 绑 6 个 agent = 1 个 client）。
- **它解决的问题我们本来就没有**：容量上限 + LRU 防的是"stateful 连接数无界"，而我们的连接数 = 每租户配置的 server 数（**个位数**）。

**因此 (b) 不是池化问题，是连接所有权问题**；上游做法反而印证了原型方向——把连接交给一个**长命 owner** 持有，而不是每个节点 task 各自持有。

### 要做的事（按顺序）

1. **先验 §5**：2.0.8 的 #2499 `fix(mcp) cleanup of cancelled MCP connections` 是否真解决了 task-affinity。
   > ⚠️ **存疑，需实测**：上游 main 的 `connect()` 现在用 `asyncio.shield(stack.aclose())`
   > 兜底，描述为 "preventing an abandoned stdio subprocess"——修的是**连接过程中被取消**，
   > **不是**"从另一个 task 关闭"。`shield` 挡不住"退出 cancel scope 的 task 不是进入它的那个"。
   > 不能按源码描述推断。
2. **若仍不解决** → 做 **"owner task 持有连接"的薄封装**（**不是**完整池）：
   owner task 独占持有，enter/use/exit 都在其内。
   **已用原型验证**（2026-09-11）：任意 task 借用、5 路并发复用、跨 task 关闭**全部通过**。
3. **不需要**：池键 / 借用超时 / 故障重建 / 优雅关闭顺序那套淘汰机制——连接数是个位数。

**涉及**：`agents/mcp_manager.py`（连接所有权改为 owner task）+ 可能新增薄的 owner 封装模块。

> 若走第 2 步，`agents/mcp_manager.py` 的 `_evict` / `close_all`（现按调用方 task 直接
> `client.close()`）须一并改——**那正是泄漏点**。

---

## 7. 已完成主体的遗留尾巴

> 三处「主体已完成、确有剩余」的项，正文已移入 §11 留痕，**只把真正还没做的部分留在这里**
> ——否则任务在"已完成"里悄悄沉掉。

| 源项 | 仍未做 | 涉及文件 |
|---|---|---|
| **Agent 配置可配置化**（v1.12 主体完成） | **tools 可见性 / 超时 / 限流**尚未纳入 DB 配置（仍硬编码在 `TOOL_REGISTRY` 的 `ToolSpec.agents`）；**按租户覆盖模型参数**（model / max_iters / 是否真实 LLM）未做 | `agents/tools.py`、`agents/registry.py`、配置加载层 |
| **真实 node_runner 接入 executor**（`383b6b7` + v5.3 批C 完成） | **L2 沙箱工具接入真实 run**——`SandboxClient` 已可用，但 runner 在真实诊断链路中尚未调用 | `agents/runner.py`、`sandbox/orchestrator.py`、`workflows/*.yaml` |
| **CMDB 生产化**（`b25dc4b` + `9b37cbe` 完成） | 数据仍是 mock：替换 `backends/cmdb.py` 的 `_SERVICES` / `_DEPENDS_ON` 为真实查询；<br>（可选）拓扑加显式环检测告警——当前 `direction=both` 能反映环但**不告警** | `aiops-datasource-mcp-server/backends/cmdb.py` |

---

## 8. 清预置 lint / 测试债

- `make lint` 目前有 ~37 个预置 ruff 错误（改动前后不变，非本次引入）
- `tests/test_sandbox.py` 5 个用例依赖本机 `~/.kube/config`（本机有 kube 时会因
  incluster 配置缺失而失败；CI 无 kube 时被 skip）
- **`tests/test_sandbox.py` 有陈旧用例**：`build_toolkit() got an unexpected keyword
  argument 'use_mock'` ——批 3 删掉该参数后测试未同步

> **实测（2026-09-11）**：本机 `pytest tests/` = **6 failed / 281 passed / 4 errors**。
> 4 errors 全为缺可选依赖（`fakeredis` / `kafka`，本机未 `make install`）；6 failed 为
> kube 依赖 + 上述陈旧用例 + `test_agent_runner` 需真实 DeepSeek key。
> **与本次改动无关**（`git stash` 前后结果完全一致）。
> **后果：回归信号目前不可信**——这是本项最该先清的理由。

---

## 9. `/agents` 端点增强

- 可选 `?role=diagnose|fix` 过滤
- 工具返回元数据（`level` L1/L2、`needs_approval`）而非只有工具名

---

## 10. 其余小项

| 项 | 说明 |
|---|---|
| 端口约定同步 | `8300` 等端口在 config.py / .env.example / README / 本地 .env 四处需人工同步，易漂移 |
| agent 无谓调用 | 实测 `query_metrics` 被调 20 次（5 个指标各一次即够）；`check_infra(namespace="default")` 传错 namespace 而返回 0 pod。prompt 已加约束，属模型行为，需持续观察 |
| `search_knowledge` 真后端 | 恒返回 `INC0001`（**每次诊断都"命中"同一条假事故**）。按 design-v5.6 §4.7.3 走租户 MCP，等接缝。**也是 §4 批 A 的弱信号依赖**（不阻塞） |
| `get_trace` 启发式环境依赖 | 词表是测试床经验，换环境可能失效（design-v5.6 §3.7.1 已声明为设计边界） |

---

## 11. 已完成（留痕）

> 只记 commit 与结论，**不保留正文**。确有余留的直接指向 §7。

- ~~Agent 配置可配置化（平台化关键）~~ → ✅ `v1.12`（2026-09-03）：DB 驱动 `agent_configs`
  + `AgentConfigResolver` 合并解析 + `/agent-configs` CRUD + agent→MCP server 绑定 + SIP
  「Agent 配置」页；见 `docs/AGENT_CONFIG_DB_zh-CN.md`。**尾巴见 §7**
- ~~真实 node_runner 接入 executor~~ → ✅ `383b6b7` + v5.3 批 C：`AgentNodeRunner` 经
  `RunService(node_runner=...)` 注入 executor/API，按 `current_tenant` 路由 per-tenant
  MCP / agent 配置；有 `DEEPSEEK_API_KEY` 即真实 LLM。**尾巴见 §7**
- ~~CMDB 生产化~~ → ✅ `b25dc4b` + `9b37cbe`（2026-09-11）：CMDB 迁至数据面 MCP
  （`locate_repo` / `get_service_topology`），删硬编码个人路径、本地 `locate_code` 与
  `cmdb=` 注入链；**租户隔离改由部署承载**，接口无 tenant 的问题由架构消除。**尾巴见 §7**
- ~~`tests/test_workspace.py` 引用不存在的 `agentflow.workspace`~~ → ✅ `67c9549`：
  不是"模块未落树"，而是 `.gitignore` 里裸写的 `workspace/` 匹配任意深度同名目录，
  把源码包整个吞掉、从未入库
- ~~Worker 不热载 agent 配置~~ → ✅ `911c7d3`：按库内指纹 TTL 热载，绑定 MCP server 无需重启
- ~~本地直连数据源实现~~ → ✅ `578ea40`（批 3）：`datasources.py` 及脚本删除，取数 MCP-only
- ~~MCP 工具列举重复握手~~ → ✅ `b8e1c88`：TTL 记忆化，单次节点执行会话 56 → 13（-80%）
- ~~审批超时 Resume 卡死 / 幂等未接线 / JWT 多租户~~ → ✅ `bb7b8ce` / `383b6b7`

