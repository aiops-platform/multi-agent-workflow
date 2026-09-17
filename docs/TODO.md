# TODO（待办清单）

> 目标：把 agentflow 从"单项目写死"演进成"可配置平台"。
> 每项带「现状 / 问题 / 目标 / 涉及文件」，方便接手。
>
> **排序原则（降序）**：① 是否阻塞上生产 / 安全 → ② 是否卡住别的活（前置关系）→ ③ 产品价值。
> **已完成主体的项不再保留正文**，只在 §12 留一行痕迹；但仍未做完的尾巴统一收在 §9。

| # | 项 | 为什么排这个位置 |
|---|---|---|
| 1 | 审批通知渠道 | 生产阻塞。human-in-the-loop **断链**，且拖垮的是**已实施**的节点级审批 |
| 2 | MCP server 部署与安全 | 生产阻塞。无镜像/无认证/凭证明文，**上不了生产环境** |
| 3 | 沙箱安全加固 | 生产阻塞（安全）。无认证无 egress，**谁能连上就能执行代码** |
| 4 | JWT 生产化（JWKS + 前端 Bearer） | 生产阻塞。**算法已可用**，缺配套；不补就切不了 JWT 模式 |
| 5 | 租户库 fail-open | 安全相邻。未注册租户**自动建库** → 资源耗尽面 + 排查噪音 |
| 6 | 动态编排（编排层） | 产品主方向，但**须人工评审批准**才能开工，故不高于上面五项 |
| 7 | AgentScope 升级评估 | **前置项**：解锁 §8，并决定 stateful 能不能开 |
| 8 | MCP 连接复用 | 修既存缺陷（stdio 子进程泄漏），前置是 §7；**经查证不做完整池** |
| 9 | 已完成主体的遗留尾巴 | 真活但零散、不阻塞，容易被忘 |
| 10 | 清预置 lint / 测试债 | 不阻塞，但债会让回归信号不可信 |
| 11 | 其余小项 | 观察项 / 长期项 |
| 12 | 已完成（留痕） | 无正文，只记 commit 与结论 |
| 13 | 新租户缺「播种」 | 新租户直接起不来诊断（`/tickets/{tid}/run` 400） |
| 14 | 分层：API ↔ Service ↔ Repository | 债，不阻塞。已有一处**重复实现**与一处**反向依赖** |
| 15 | `datasource/` 例外：保留但立规矩 | 例外**合理**；含一个**无鉴权端点**（只读低危） |
| 16 | ~~MCP 工具绑定只有 server 级粒度~~ | **先不做**——首版证据已被推翻；复核只剩 2 处零星越界，**先改 prompt 即可** |
| 17 | ~~`scope` 不输出消歧字段~~ | ✅ 已解决——**真因是 Worker 未重启**，不是 prompt |
| 18 | **改代码不热载**：Worker 只认库内指纹 | 每次改 prompt/schema 都会静默用旧版——已在实测中骗过一次 |
| 19 | ~~`rca` 的 `join: any`~~ | ✅ 已修——**根因节点从来没拿到过取证输出**（五维摘要恒为 None），见下 |

> **中断语义（halt）不在本清单里**——它已实施并实测通过（`8841d20` / `2821ac5`），
> 约定见 `CLAUDE.md` 约束 3 与 `docs/E2E_VERIFICATION_zh-CN.md` §四验收点。

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

## 4. ⭐⭐ JWT 生产化：JWKS + 前端 Bearer（阻塞 JWT 模式上线）

> 2026-09-14 记录。**算法已可用**（RS256 配置即可，见 `DEPLOYMENT_zh-CN.md` §5.1），
> 缺的是让 JWT 模式能真正跑起来的配套。

### 现状

鉴权只有两种模式（`api/auth.py`）：`AGENTFLOW_JWT_SECRET` 非空 = JWT，为空 = dev
（`X-Tenant-ID`）。当前跑的是 dev。切 JWT 卡在三处：

| # | 缺什么 | 后果 |
|---|---|---|
| 1 | **前端不发 `Authorization`** | 切到 JWT 后所有请求 401，界面全空 |
| 2 | **无 JWKS**（自动取公钥 / 按 `kid` 匹配 / 轮换） | 只能手工分发 PEM 塞环境变量；换钥要重启服务 |
| 3 | **无签发侧**（Gateway / IdP） | 本仓只验签不签发（`grep jwt.encode` 零命中） |

### 目标

- 前端 `src/api/agentflow.js` 的 `createClient` 加认证头（已支持函数式 `headers`），
  token 存取 + 过期处理 + 401 兜底
- `jwt_algorithm=RS256` 时支持 `AGENTFLOW_JWKS_URL`：启动拉取 + 缓存 + 未知 `kid` 重取
- 密钥装载从"塞环境变量"改为可从文件路径读（PEM 多行，env 很别扭）

### 前置 / 注意

- **切之前必须确认 `AGENTFLOW_SECRET_KEY` 显式且稳定**：它若未设置就从 `jwt_secret`
  派生，改 `jwt_secret` 会导致所有租户 `db_ref` 解不开（见 `E2E_VERIFICATION_zh-CN.md` §8.5）
- 建议先做完存量租户库迁移再切（否则"哪个租户"同时受旧库绑定与 claim 派生两层影响）

## 5. ⭐⭐ 租户库 fail-open：未注册租户会自动建库

> 2026-09-14 记录。修「provision 真的建库」时引入的取舍，实测已数次触发。

### 现状

`router._resolve_ref` 对**未注册**的租户 id 回退到默认策略 → `ensure_tenant_database`
给它建一个**空库**（与 sqlite 自动建文件的行为一致）。

dev 模式下 `auth.py` 缺省租户是 `"local"` —— 于是**任何不带 `X-Tenant-ID` 的请求**
（手工 curl、没更新的客户端）都会创建一个 `agentflow-<任意名>` 空库。实测中
`agentflow-local` 反复出现，清了三次。

### 影响

- **无界增长**：任意 header 值都产生一个 database，是个资源耗尽面
- **排查噪音**：多出来的空库让人以为有租户没清

### 目标（二选一）

- **fail-closed（推荐）**：管理库存在且租户未注册 → 拒绝（404/403），不建库。
  JWT 模式下租户来自签名 claim，本就不该有"未注册"；dev 模式下显式报错也比静默建库好。
- **可配默认租户**：加 `AGENTFLOW_DEFAULT_TENANT`，让缺省不再是硬编码的 `"local"`。

### 涉及文件

`agentflow/statestore/router.py`（`_resolve_ref`）、`agentflow/api/auth.py`、
`agentflow/config.py`

## 6. ⭐⭐ 动态编排（编排层）—— **未实施**，设计见 `docs/design-v5.6.md` §4

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

- **`search_knowledge` 真后端**（见 §11）：§4.2 复杂度判定的"知识命中"信号现为 mock，
  只能当弱信号；真后端走租户 MCP，**独立排期**，不阻塞批 A；
- **审批通知渠道**（见 §1）：`human-in-the-loop` 若审批人收不到通知，
  「审批必然超时」会使计划审批退化成形式。

---

## 7. ⭐ 评估 AgentScope 2.0.3 → 2.0.8 升级

> 2026-09-11 记录。**不是**为了追新，而是因为落后版本已经卡住了两处设计空间。
> **排在第 7 位是因为它是 §8 的前置**——先看上游修没修，再决定 §8 要不要自建。

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

## 8. MCP 连接复用（先验 #2499，再决定要不要自建）

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

## 9. 已完成主体的遗留尾巴

> 三处「主体已完成、确有剩余」的项，正文已移入 §12 留痕，**只把真正还没做的部分留在这里**
> ——否则任务在"已完成"里悄悄沉掉。

| 源项 | 仍未做 | 涉及文件 |
|---|---|---|
| **Agent 配置可配置化**（v1.12 主体完成） | **tools 可见性 / 超时 / 限流**尚未纳入 DB 配置（仍硬编码在 `TOOL_REGISTRY` 的 `ToolSpec.agents`）；**按租户覆盖模型参数**（model / max_iters / 是否真实 LLM）未做 | `agents/tools.py`、`agents/registry.py`、配置加载层 |
| **真实 node_runner 接入 executor**（`383b6b7` + v5.3 批C 完成） | **L2 沙箱工具接入真实 run**——`SandboxClient` 已可用，但 runner 在真实诊断链路中尚未调用 | `agents/runner.py`、`sandbox/orchestrator.py`、租户库的 `workflows` 表 |
| **CMDB 生产化**（`b25dc4b` + `9b37cbe` + 实体图谱化完成） | ⚠️ **换了载体 ≠ 换了数据源**：`_SERVICES` / `_DEPENDS_ON` 字面量已删除，但实体文件里的 **10 个服务 / 13 条边仍是种子数据**——仍需接真实 CMDB 同步（文件载体、schema 校验、引用完整性、热重载都已就位，缺的只是数据来源）；<br>**两处失真由种子派生而来，录入真实数据时应一并纠正**：① Portfolio 由 `namespace` 派生——namespace 是 k8s 部署分组不是业务域，`common` 是装着两个不同 owner 服务的杂物筐；② `tier1` 标签由 `criticality==critical` 派生；<br>**Event 路径空转**：Incident / Change 节点当前为空（覆盖层机制已通，缺数据），故 `infer_candidate_services` 恒 `degraded=true`，「问题 → 事件 → 应用」这条路径尚未在真实数据上验证过；<br>（可选）拓扑加显式环检测告警——当前 `direction=both` 能反映环但**不告警**（图谱化后实体文件允许含环，此风险未变） | `aiops-datasource-mcp-server/src/.../data/cmdb-entities.json`、`docs/cmdb-entities.md` §5 |

---

## 10. 清预置 lint / 测试债

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

## 11. 其余小项

| 项 | 说明 |
|---|---|
| 端口约定同步 | `8300` 等端口在 config.py / .env.example / README / 本地 .env 四处需人工同步，易漂移 |
| agent 无谓调用 | 实测 `query_metrics` 被调 20 次（5 个指标各一次即够）；`check_infra(namespace="default")` 传错 namespace 而返回 0 pod。prompt 已加约束，属模型行为，需持续观察 |
| `search_knowledge` 真后端 | 恒返回 `INC0001`（**每次诊断都"命中"同一条假事故**）。按 design-v5.6 §4.7.3 走租户 MCP，等接缝。**也是 §4 批 A 的弱信号依赖**（不阻塞） |
| `get_trace` 启发式环境依赖 | 词表是测试床经验，换环境可能失效（design-v5.6 §3.7.1 已声明为设计边界） |

---

## 12. 已完成（留痕）

> 只记 commit 与结论，**不保留正文**。确有余留的直接指向 §7。

- ~~Agent 配置可配置化（平台化关键）~~ → ✅ `v1.12`（2026-09-03）：DB 驱动 `agent_configs`
  + `AgentConfigResolver` 合并解析 + `/agent-configs` CRUD + agent→MCP server 绑定 + SIP
  「Agent 配置」页；见 `docs/AGENT_CONFIG_DB_zh-CN.md`。**尾巴见 §9**
- ~~真实 node_runner 接入 executor~~ → ✅ `383b6b7` + v5.3 批 C：`AgentNodeRunner` 经
  `RunService(node_runner=...)` 注入 executor/API，按 `current_tenant` 路由 per-tenant
  MCP / agent 配置；有 `DEEPSEEK_API_KEY` 即真实 LLM。**尾巴见 §9**
- ~~CMDB 生产化~~ → ✅ `b25dc4b` + `9b37cbe`（2026-09-11）：CMDB 迁至数据面 MCP
  （`locate_repo` / `get_service_topology`），删硬编码个人路径、本地 `locate_code` 与
  `cmdb=` 注入链；**租户隔离改由部署承载**，接口无 tenant 的问题由架构消除。**尾巴见 §9**
- ~~`tests/test_workspace.py` 引用不存在的 `agentflow.workspace`~~ → ✅ `67c9549`：
  不是"模块未落树"，而是 `.gitignore` 里裸写的 `workspace/` 匹配任意深度同名目录，
  把源码包整个吞掉、从未入库
- ~~Worker 不热载 agent 配置~~ → ✅ `911c7d3`：按库内指纹 TTL 热载，绑定 MCP server 无需重启
- ~~本地直连数据源实现~~ → ✅ `578ea40`（批 3）：`datasources.py` 及脚本删除，取数 MCP-only
- ~~MCP 工具列举重复握手~~ → ✅ `b8e1c88`：TTL 记忆化，单次节点执行会话 56 → 13（-80%）
- ~~审批超时 Resume 卡死 / 幂等未接线 / JWT 多租户~~ → ✅ `bb7b8ce` / `383b6b7`
- ~~`/agents` 端点增强~~ → ✅ 2026-09-14（B3）：`/agents` 补 role/enabled/origin/
  mcp_server_ids/bound_servers，`tools` 拆成 `local_tools`（**含 level/needs_approval**，
  前端据此推导自治 tier）与 `mcp_tools`；新增 `GET /agents/{name}`（完整 prompt/schema）
  与 `GET /agents/stats`（按 agent 聚合，**空样本为 null 而非 0**）
- ~~工单不落库~~ → ✅ 2026-09-14（B2，`eead105`）：新增 `ticket_store` + 4 个端点
  （含 `POST /tickets/{tid}/run` 组合端点）；工单成为一等实体
- ~~run 不可观测~~ → ✅ 2026-09-14（B1，`84271fc`）：新增 `GET /runs` 列表；
  `GET /runs/{id}` 补 `inputs`/时间戳/节点 `error`·`duration_ms`·`attempts`
- ~~PG 模式租户共库~~ → ✅ 2026-09-14（`d187d46`）：`provision` 真的建独立库
  （`agentflow-{tenant}`），修掉三张无 tenant_id 列的控制面表跨租户可见
- ~~工单落共享库~~ → ✅ 2026-09-14（`166c5e4`）：`ticket` 纳入 `TenantStores` 路由
- ~~`_agent_config_provider` 作用域错误~~ → ✅ 2026-09-14（`5ab1494`）：
  Router 模式下任何 agent 节点都因 NameError 失败（**main 上既有 bug**）

---

## 13. ⭐ 新租户初始化缺「播种」：workflow 与数据面绑定

> 2026-09-16 记录。做 scope 节点的 E2E 时踩到：**改完 workflow YAML，run 跑的还是旧流程**。

### 现状（源码事实）

**workflow 的唯一真源是数据库，不是仓库里的 YAML**：

| 动作 | 路径 | 证据 |
|---|---|---|
| 存 | `POST /workflows` → `workflow_store.py` | `INSERT INTO workflows(id, name, yaml, created_at)` |
| 用 | run 时从库读 | `app.py:697` `cs.workflow.list()` → `:704` `get(wid)` → `:398` `Workflow.load_yaml(wf_row["yaml"])` |
| 仓库 `workflows/*.yaml` | **完全不参与运行时** | 全仓 grep `workflows/*.yaml` / `glob` / `seed_workflow` → **零命中**；Makefile、`scripts/` 里也没有导入动作 |

而 `tenantctl provision` **只建库建表、不播种任何数据**：
`ensure_tenant_database`（建 database）→ `mgmt.upsert_tenant`（管理库记录）→
`router.get()`（连接触发幂等建表）——到此为止。

### 影响

- **新租户起不来诊断**：`workflows` 表为空 → `POST /tickets/{tid}/run` 直接 400
  （"库里没有已保存的 workflow，无法发起诊断"）
- **改 workflow 会静默不生效**：改了仓库 YAML 却忘了 `PUT /workflows/{wid}`，
  run 跑的还是旧流程——**没有任何提示**。本次就差点如此：YAML 改完、加载校验通过、
  agent 也加好了，唯独库里那份是旧的，跑出来会**像"新功能没生效"**，而真因是流程里
  根本没有那个节点
- **同样的问题在数据面**：`mcp_servers` 表空 + `agent_configs.mcp_server_ids` 未绑定
  → agent 拿不到任何工具（`mcp_server_ids` 是两态语义：NULL/`[]` = 无 server）

### 要做

1. **`provision` 时播种 workflow** —— ⚠️ **播种源需要先定**，因为仓库里的
   `workflows/*.yaml` 已于 2026-09-16 **删除**（见 §13 开头：它们不在运行时链路上）。
   三个候选，**选之前不要动手**：
   - **(a) 显式 seed 目录**：如 `agentflow/seed/workflows/*.yaml`，**命名上就要让人看出
     它是种子不是真源**。代价：又多了一处 YAML，仍可能被误读成"改了生效"。
   - **(b) 租户间复制**：从一个"参考租户"导出（`GET /workflows` → 另一个库 `POST /workflows`）。
     好处：**只有一份真源（数据库）**，不引入第二载体。代价：需要有个 reference 租户当样板。
   - **(c) 由运维显式导入**：provision 只报错提示"库里没有 workflow，请先 POST /workflows"。
     最小改动，但没有解决"新租户开箱不可用"。
   > **倾向 (b)**：它与"真源唯一"的原则一致——(a) 会把刚删掉的第二载体又请回来。
2. **数据面绑定也要播种**：`mcp_servers` 注册 + 各 agent 的 `mcp_server_ids`。
   ⚠️ 这部分 **URL 是环境相关的**（每租户一个 MCP server，见 v5.3 部署模型），
   不能硬编码进代码——需要在 `tenants.yaml` 里声明该租户的数据面 server 列表
3. **补一个「同步」动作**：改完 workflow 后一条命令推给目标租户，而不是手工 `PUT`

### 涉及文件

`agentflow/tenantctl.py`（`provision`）、`agentflow/api/workflow_store.py`、
`agentflow/api/mcp_store.py`、`agentflow/api/agent_store.py`

> **与版本冻结的关系**：run 用 snapshot，改 YAML **不影响已发起**的 run（这是对的）；
> 但新租户 / 新建的库必须有一份初始的——缺的就是这一步。

---

## 14. 分层：API ↔ Service ↔ Repository（+ 执行引擎）

> 2026-09-17 记录。**分层定义（本项目采用）**：
>
> | 层 | 定义 | 判据 |
> |---|---|---|
> | **API** | 带 FastAPI、**经外部 HTTP 访问**的接口 | 文件里有 `from fastapi` |
> | **Service** | **进程内**调用，只做逻辑组装，**不含 FastAPI** | 无 fastapi、无 `api/` |
> | **Repository** | 数据访问 | 被 service 调用，不反向依赖 |
>
> 调用方向：**API → Service → Repository**。
>
> ⚠️ 本项前两版分别基于「未 pull 的副本」与「执行侧 vs 网页侧」两种框子，**均已作废**。
> 本版按上面的分层定义重做，全部结论有实测支撑。

### 一、现状分类（实测）

**① 谁真的带 FastAPI**——全包只有 **2 个文件**：

```
api/app.py     5 处   ← 37 个端点全在这
api/auth.py    2 处   ← 租户上下文依赖
```

（`datasource/*` 里那两处是**注释**——`prometheus.py` 原文就写着"本模块零 FastAPI 依赖"。
不能靠 grep 字符串判层，得看是不是真的 import。）

**② 各层现状**：

| 层 | 现状 | 判定 |
|---|---|---|
| API | `api/app.py`、`api/auth.py` | ✅ 干净 |
| Service | `service.py`（RunService） | ✅ 干净——不 import fastapi，也不 import `api/` |
| Repository | `statestore/*`（含 `router.py`）、**`api/*_store.py` ×5** | ❌ 一半被错放在 `api/` 下 |
| （三层之外）执行引擎 | `executor/`、`agents/`、`workspace/`、`sandbox/`、`core/` | 不在这个分层里，见下 |

### 二、四处违反（按真实程度排序）

**① （最大）API 层直接编排一切。** `api/app.py`（1600+ 行）import 了 **11 个顶层包**：

```
agents approval config core datasource executor lock queue service statestore tenants worker
```

其中包括 `from ..worker import WorkerPool`——`queue=memory` 模式下 API 内联拉起 worker
（这是设计，见 README），**但也说明这个文件同时在当端点层与当编排层**。
它 import 了 `service`，可大量编排逻辑仍写在端点文件里。

> **判据 1「`api/` 只 import service 与 fastapi」当前被大面积违反。**
> 危害不是"难看"：端点文件里混着执行编排，任何改执行语义的人都要在 1600 行里翻。

**② Repository 被放在 API 层**（`api/*_store.py` ×5）：

```
api/app.py                37 端点   ← API 层
api/auth.py                0 端点   ← API 层（FastAPI 依赖）
api/agent_store.py         0 端点   ← Repository
api/management_store.py    0 端点   ← Repository
api/mcp_store.py           0 端点   ← Repository
api/ticket_store.py        0 端点   ← Repository
api/workflow_store.py      0 端点   ← Repository
```

后果是**依赖方向反了**：`statestore/router.py`（Repository）→ `api/`（API 层）。
今天零代价，但锁死未来——哪天某个 store 需要 import `app.py` 里的东西，
Worker 进程就会被真的拖上整个 web 栈（FastAPI / starlette / uvicorn），**且不会有任何提示**。

**③ Worker 绕过 Service，直接调 Repository + 执行引擎**——而且**已经造成重复实现**：

`service.py:231` 与 `worker.py:212` 各有一份 `_mark_cancelled`，**逐行近乎相同**
（唯一差别是 Worker 那版自己 `resume_executor` 重建 executor）：

```python
for nid, st in ex.node_states.items():
    if st.get("status") in TERMINAL: continue
    ex.node_states[nid] = {"status": "cancelled", "output": None}
    await store.put_node(run_id, tenant_id, nid, ex.node_states[nid])
await store.update_run(run_id, status="cancelled")
```

Worker 那版的 docstring 自己写着「**与 RunService.stop_run 同语义**」——**两处要同步维护**。
这是分层被绕过的**实际代价**，不是理论担忧。

**④ API 层直接做数据访问**：`api/app.py` 的 `/app-indicators` 直接调 `datasource/`，
跳过了 Service。按定义 `datasource/` 是取数适配器（repository 性质），
而 API 应只调 Service。

> 这条是**边界情况**：该端点就是个纯透传快照，为它加一层 service 可能只是仪式。
> 要么补一层，要么在 §15 里显式记为"已知例外"。**不要**默认它没问题。

### 三、与「进程归属」的关系（两把尺子，都要用）

分层（角色）与进程归属（跑在哪）**是正交的两件事**，同一模块两个答案都要对：

| 模块 | 层 | 跑在 |
|---|---|---|
| `api/app.py`、`api/auth.py` | API | 仅 API 进程 |
| `service.py` | Service | **两个进程都要**（Worker 也必须经它，见违反 ③） |
| `statestore/*`、5 个 `*_store.py` | Repository | 两个进程都要 |
| `executor/`、`agents/`、`workspace/`、`sandbox/` | 执行引擎（三层之外） | 仅 Worker |
| `datasource/` | 取数适配器 | 仅 API 进程 |

> 实测方法（可复核）：按入口点做传递导入闭包。当前 **Worker 加载的包是 API 的真子集**，
> 且 Worker 会加载 `api` 包（违反 ② 的直接证据）。

### 四、目标

```
agentflow/
  api/          ← 只放 FastAPI 端点 + 认证依赖
  service/      ← 逻辑组装（RunService + 执行编排）
  repository/   ← 数据访问（statestore/* + 5 个 *_store.py 迁入）
  (其余不变)     ← 执行引擎 / 基建，不属于三层
```

**强制判据**（可写成测试，比约定可靠）：

1. `api/` **只** import service 与 fastapi
2. `service/` 与 `repository/` **不得** import fastapi，也**不得** import `api/`
3. Worker **必须经 service**，不得直接调 repository（违反 ③）
4. Repository **不得**依赖 service / api（违反 ②）

### 五、分两批

- **批 A（小、对症）**：5 个 store 从 `api/` 挪到 repository 层；把 Worker 的
  `_mark_cancelled` 换成调用 service —— 违反 ② ③ 一起消。约 20 处 import。
- **批 B（大、表达意图）**：按上面的目标建目录。**只有批 A 做完、且确有更多
  越界出现时才值得做**——单纯为了好看搬 `core/` / `agents/`，diff 与收益不成比例。

> **不要**顺手拆 `app.py`。37 个端点挤一个文件确实大，但拆它没有分层收益、
> 只有 review 噪音——那是另一件事。

### 涉及文件

批 A：`agentflow/api/{agent,management,mcp,ticket,workflow}_store.py`、
`agentflow/statestore/router.py`、`agentflow/worker.py`、`agentflow/service.py`、
`agentflow/api/app.py`，以及对应的 8 个测试文件

---

## 15. `datasource/` 架构例外：**保留，但要立规矩**

> 2026-09-17 记录。前身是 §14 首版里对它的批评，**那些批评基于未 pull 的旧副本，已作废**。
> 重新核实后结论反转：**例外是合理的。**

### 为什么合理（三个结构性差异，不是"少几个指标"）

`GET /app-indicators` 的真实消费者是遗留前端 Smart Inspection 页面
（`js/app.js:initSmartInspection`，`76e9adc` 起走相对路径 `/agentflow`，5 秒轮询）。

| | MCP（`aiops-datasource-mcp-server`） | 这个页面要的 |
|---|---|---|
| 形态 | `query_range` **时序** | **瞬时快照**，一次拉全表 |
| 指标 | 5 个领域语义（cpu/memory/**disk_percent**/error_rate/p95） | 还要 **blockIO / netIO / netIn / netOut** |
| 元数据 | — | `owner` / `serviceType` 来自 K8s Deployment label |

⚠️ **注意 `disk` 那一列是 `disk: item.blockIO`**——页面显示的其实是**块设备 IO**，
不是 MCP 的 `disk_percent`（容量）。**同名不同物**，硬套会更糟。

### 与 §14 的关系

本包**只被 `api/app.py` 调用**——即 **API 层直接做数据访问，跳过了 Service**
（§14 违反 ④）。要么补一层 service，要么把"这是已知例外"写在这里，**不要默认它没问题**。

### 三个仍然成立的问题

1. **端点无鉴权**（`app.py:1582`）。JWT 模式下其余 34 个端点都要 `get_tenant_context`，
   只有它不要——而 `tests/test_app_indicators_api.py` 还专门写了一条测试把它
   **「回归锁定」**（第 56 行"刻意不带任何 Header / Token"）。
   **把一个安全缺口锁成了不可回退的约定。** 加了真实消费者之后这条更值得处理。
2. **收编期限没有正文条目**：代码里三处 `TODO(v5.7)`，`docs/TODO.md` 里搜不到。
   例外会因此永久化。
3. **重复实现已知会分叉**：`container!="POD"` 在测试床集群上算错（sandbox 序列的
   `container` 标签缺失，CPU 接近翻倍），正解是 `container!=""`。
   同一件事两处实现，一边对一边错——**而且错的是 MCP 侧（我们自己）**。

### 目标

- **保住例外**（它服务的是 MCP 覆盖不了的需求），但**立规矩**：
  - 端点鉴权：要么加 token（并同步改遗留页面），要么在部署侧限制来源；
    **至少不要用测试把"无鉴权"锁死**
  - 把内联的 `TODO(v5.7)` 变成 TODO 列表里看得见的一条
- **顺手修 MCP 侧的 `container!=""`**（与例外无关，是我们自己的缺陷）

### 涉及文件

`agentflow/api/app.py`（`/app-indicators`）、`agentflow/datasource/*`、
`tests/test_app_indicators_api.py`；
MCP 侧 `backends/prometheus.py`（`_sel()`）

---

## 16. MCP 工具绑定只有 **server 级**粒度

> 2026-09-17 记录。做「triage 交出数据工具」时发现：能做的只有**全给或全不给**。

### 现状

`agent_configs.mcp_server_ids` 是**按 server 绑**的，而 `aiops-datasource-mcp-server`
把 **9 个工具全放在一个 server 上**：

| 类别 | 工具 |
|---|---|
| 数据面 | `query_logs` `get_trace` `query_metrics` `check_infra` `describe_pod` |
| CMDB/图 | `get_service_topology` `locate_repo` `query_entity_graph` `infer_candidate_services` |

于是绑了这个 server 的 agent **一律拿到全部 9 个**。实测 `otr` 租户里 **8 个 agent
全绑了同一个 server**（批量绑的），每个都拿全套。

AgentScope 的 `Toolkit(mcps=[...])` **没有按工具过滤的入口**；`ToolPolicy.allowed_tools`
**没有任何消费方**，其 docstring 还明说「数据源与 CMDB 工具已迁 MCP——其放行由 MCP 侧
承担，不在此处枚举」。**所以当前没有"限制某个 agent 拿哪些 MCP 工具"的机制。**

### 已做的（2026-09-17）

**triage 解绑**（`mcp_server_ids` → NULL）——它本就不该有工具（症状分类只看工单文本），
所以"全不给"恰好就是正确答案。配套改了 prompt 与 `_SERVICES_RULE`。

### ⚠️ 首版的实测证据**已被推翻**（2026-09-17 晚）

首版记录引了三例"agent 越界调用工具"，并据此得出"**prompt 禁止不住行为，只有工具面能禁**"。
**那些证据不成立**——它们全部来自**旧 prompt**（Worker 没重启，见 §17）。

重启 Worker、新 prompt 真正生效后重跑：

| 节点 | 旧 prompt 下 | 新 prompt 下 |
|---|---|---|
| `log-analyst` | 调 `get_trace`（越界） | ✅ 只有 2 次**按服务**的 `query_logs` |
| `trace-analyst` | 先调**无 service 的宽** `query_logs` | ✅ 只有 1 次 `get_trace`（用工单 trace_id） |

**两个越界都消失了。**

### 所以本项降级：**先不做**

原理仍然站得住（工具面是**强制**，prompt 是**请求**；`triage` 那次确实是靠**解绑 server**
禁住的，而它的 prompt 当时明确要它查数据）。但在拿不出"新 prompt 下仍需强制"的实例之前，
拆 server 的代价（两个 FastMCP 实例 + 路径 + lifespan + 测试，且让边界变成部署问题）
换不回可验证的收益。

### 新 prompt 下的复核（2026-09-17，2 次 run）

**越界仍存在，但形态与严重度完全变了：**

| | 旧 prompt | 新 prompt（2 次 run） |
|---|---|---|
| 涉及节点 | `log-analyst` / `trace-analyst` | `root-cause`（**2/2**）、`infra-locator`（1/2） |
| 形态 | **成体系**——`trace-analyst` **每次都**先查宽 `query_logs`（旧 prompt 就是这么要求的） | **零星**——每次不同，且**不是"替别人干活"** |
| 严重度 | 高（两者工作实质重叠） | 低 |

其余 11 个节点**两次都完全合规**（含 `triage` 零调用、`scope` 只调图工具）。

**⚠️ 对 `root-cause` 的解读与上面不同**：它调 `get_service_topology` **两次都出现，
而它的 prompt 并没有禁止**——只写了"可用全部 5 个数据工具自行取证"。
所以这更像**设计缺口**（prompt 与工具面没对齐）而不是模型乱来：

- 它的入参里**已经有** `scope_primary` 与 `failing_service`（供交叉判断）
- 但它想自己看拓扑时，**唯一的手段就是调 `get_service_topology`**——而那工具在它列表里

**这与旧 prompt 下 `trace-analyst` 先查日志是同一性质。**

### 建议（优先级高于拆 server）

**先改 prompt，别拆 server。** 例如给 `root-cause` 写明：
「拓扑关系从入参的 `scope_primary` / `failing_service` 拿，**不要自己查**」。
成本一行，且直接对着实测到的那个稳定缺口。

真要根治（工具面强制）时，注意下面那条更省的路：

### 缺口

`service-scoper` **该有**图工具（design-v5.7 §3.5 点名 `query_entity_graph` +
`get_service_topology`）**但不该有**数据工具——现在它全有，可以自己跑去查日志
（上一轮那次伪归因就是这么发生的）。

### 目标（二选一）

1. **拆 server**：同一个代码库起两个 FastMCP 实例（`aiops-datasource` 数据面 /
   `aiops-cmdb` 图），agent 按需绑。边界最清楚，但要改部署与注册。
2. **加 per-agent 工具 allowlist**：`agent_configs` 加一列 + `build_toolkit` 过滤。
   改动小，但要在 AgentScope 的 Toolkit 之外自己拦一层（它没有原生入口）。

> 倾向 1：**边界由部署承载**是本项目既有做法（v5.3 租户隔离就是这么做的），
> 而且拆完之后"scope 能查日志吗"在配置上一眼可见，不需要读代码。

### 涉及文件

`agentflow/agents/mcp.py`（`build_toolkit`）、`agentflow/api/agent_store.py`（如需加列）、
`aiops-mcp-servers/servers/aiops-datasource-mcp-server/server.py`（拆实例）

---

## 17. `scope` 不输出业务域消歧字段 —— **已解决，真因是 Worker 未重启**

> 2026-09-17。⚠️ **本项首版诊断是错的**（当时判为"prompt 太长把新字段淹没"），下面是更正后的记录。

### 真实原因

**新 prompt 从来没送达模型。**

`config_sync.py` 的热载指纹是 `(行数, MAX(updated_at))`——**只看数据库的行**。
而改 `prompts.py` 改的是**代码**，指纹不变 → Worker 不重建 resolver；
而 `SYSTEM_PROMPTS` 是 import 时的模块级字典，**进程不重启就不更新**。

实测证据：Worker 里跑的是 **1728 字符的旧版 prompt**，连 `business_paths` 这个词都没有；
新版是 3427 字符。**字段从来没被要求过，当然不会输出。**

**为什么极难发现**：API 侧（`uvicorn --reload`）会重新 import，所以
`GET /agents/service-scoper` **能看到新 prompt**——你以为改对了，实际 Worker 里是旧的。
**两侧不一致，且没有任何报错。**

### 修复

1. **重启 Worker**（真正起作用的那一步）
2. 顺带把 scope 的 prompt 重构了：把「输出契约」从流程规则里**拎出来单独成段**
   （`## 二、输出契约（逐字段填，缺一不可）`）。**这一条不是必需的修复**——
   实测证明只要 prompt 送达了就会输出；保留它是因为读起来更清楚，
   但**不要以为它解决了问题**。

### 验证（重启 Worker 后重跑）

```
matched_domains : [{'name': 'customer-server-journey', 'type': 'journey', ...}]   ✓
ambiguous       : False                                                          ✓
  order-service    evidence_source='ticket_cmdb_ci'  business_paths=有  in_domain=True   ✓
  payment-service  evidence_source='topology'        business_paths=有  in_domain=True   ✓
  gateway-service  evidence_source='topology'        business_paths=有  in_domain=False  ✓
```

### 遗留

**这个坑会在每次改 prompt/schema 时重演**——已提成独立条目 **§18**（含修法与短期兜底）。
判据与重启命令见 `docs/E2E_VERIFICATION_zh-CN.md` §6.5。

### 相邻问题：`scope` 会自造零证据候选

首版记录里有这条，**未被本轮验证覆盖**（可能同样是旧 prompt 所致，也可能不是）。
新 prompt 已加禁令 1「不要自己添加工具没返回的候选」。

---

## 18. 改代码不热载：Worker 只认「库内指纹」

> 2026-09-17 记录。这个坑在 §17 里**已经骗过一次**——两个场景各跑一轮都得出错误结论，
> 最后靠读 trace 里发给模型的 system prompt 才发现。**强烈建议优先做**（成本小、收益直接）。

### 现状

`agents/config_sync.py` 的热载指纹是：

```python
fingerprint = (_signature(agent_rows), _signature(mcp_rows))
# _signature = (行数, MAX(updated_at))
```

**它只看 `agent_configs` / `mcp_servers` 两张表的行**。于是：

| 改了什么 | 会热载吗 |
|---|---|
| 页面上改 agent 配置 / 绑 MCP server | ✅ 会（行的 `updated_at` 变了） |
| **改代码里的 `SYSTEM_PROMPTS` / `AGENT_SCHEMAS`** | ❌ **不会** |

后者不生效的机理：Worker 不重建 resolver，而 `SYSTEM_PROMPTS` 是 **import 时的模块级
字典**——进程不重启就不更新。

### 为什么它极其难发现

**API 侧（`uvicorn --reload`）会重新 import**，所以 `GET /agents/{name}` **能看到新 prompt**。
于是：

```
你以为：改了 prompts.py → API 返回新的 → 生效了 ✅
实际是：Worker 里跑的还是旧 prompt，run 的行为完全没变
```

**两侧不一致，且没有任何报错、没有任何日志。**

### 实测代价

给 `service-scoper` 加了四个输出字段、改了两轮 prompt、跑了两个场景的 E2E——
都发现"字段没输出"。中间得出过两个**错误结论**（"prompt 太长把新字段淹没"、
"prompt 禁止不住 agent 越界"），并据此写了 TODO（§16 首版、§17 首版）。

**全部源于这一个坑。**

### 目标（二选一）

1. **把代码版本纳入指纹**：如把 `SYSTEM_PROMPTS` / `AGENT_SCHEMAS` 的哈希拼进
   `fingerprint`。改动集中在一处，且保留"改了就是改了"的语义。
2. **去掉 resolver 这层缓存**（若确认热载的收益不抵复杂度）。

> 倾向 1：热载本身是**对的**（变更生效有 ≤5s 延迟、换来不反复重建 MCP client），
> 不该因为它没覆盖代码就整个拿掉。

### 短期兜底

**改了 prompt/schema 就重启 Worker**，并在核对前**先比对两侧**：

```bash
# trace 里 kind=llm_call 的 payload.messages[0].content[0].text
# 与 GET /agents/{name} 返回的 system_prompt 逐字对比，不一致就是 Worker 旧了
pkill -f "agentflow.worker"
cd <backend> && nohup ./venv/bin/python -m agentflow.worker --tenant otr > /tmp/worker.log 2>&1 &
```

见 `docs/E2E_VERIFICATION_zh-CN.md` §6.5。

### 涉及文件

`agentflow/agents/config_sync.py`（指纹计算）、`agentflow/agents/agent_config.py`
（resolver 构建）

## 19. ~~`rca` 的 `join: any`~~ ✅ 已修复（2026-09-17 晚，`4691832`）

> 2026-09-17 记录。**严重**：这不是"偶尔漏一条"，而是**每一次 run 都漏**——
> 从有这两条 workflow 起就没生效过。发现路径：给 `rca → halt` 接线后做 E2E 验证，
> 顺手核对 halt.reason 来自谁，结果发现 rca 的入参**全是 None**。
>
> **修复与实测见文末「✅ 已修复」一节**（含一个同源缺陷：`rca.summary` 恒缺失，
> 以及"`schema 里有 / prompt 模板里没有`"这条通用判据）。

### 现象（实测数据）

`order-service-quotation-print-fail` 的一次正常 run（`run_8df33e75f4`），
各节点 `cp.started_at`：

| 节点 | started_at | ended_at |
|---|---|---|
| know | 09:23:20.064 | 09:23:24.410 |
| **infra / logs / metrics / rca / trace** | **09:23:27.677**（同一微秒） | — |
| trace | 〃 | 09:23:29.291 |
| infra | 〃 | 09:23:29.822 |
| logs | 〃 | 09:23:30.122 |
| metrics | 〃 | 09:23:30.394 |
| **rca** | 〃 | **09:23:38.203** |
| locate | 09:23:38.216 | 09:23:43.195 |

`rca` 与它的四个取证上游**同波启动**——rca 完成时它们才刚跑完一半。
rca 落库的 `params` 印证了这一点（`run_8df33e75f4` / `run_70d386bbac` / `run_8bd...`）：

```
logs None · trace None · metrics None · infra None · code None · know [ok] · scope_primary None
```

**五个取证维度全 None，只有 `know` 有值。**

### 成因

```yaml
  rca:
    agent: root-cause
    # ← 没有 join: all / required_edges
edges:
  - { from: logs,  to: rca }
  - { from: trace, to: rca }
  ...
  - { from: know,  to: rca }   # ← know 只依赖 triage，早一波就绪
```

`join` 默认 `any`（`core/dag.py:66`），而 `know` 的入边是 `triage → know`——
`triage` 一完成，`know` 就绪并跑掉，于是 `rca` 在**下一波**就被判定为 ready，
与 `logs/trace/metrics/infra` **同波并发**。`params` 在节点启动时解析，
此刻四大取数节点还是 PENDING → 全部解析成 `None`。

### 为什么一直没被发现

`root-cause` 自己**带全部 5 个数据工具**（YAML 注释：「root-cause 也会自行取证」），
所以它自己又查了一遍，结论看起来仍然合理——**只是那条「把五维摘要交给 rca 做交叉判断」
的设计从来没生效过**。没有任何报错、没有任何日志。

同图里的 `locate` 恰恰做了正确示范（`join: all` + `required_edges`，注释还写明了
「默认 join: any 会让它在 triage 一完成就被调度」）——**同一个坑，一个躲过了，一个没躲过**。

### 影响面

1. **诊断质量**：rca 无法交叉验证五维证据，也无法做 design-v5.7 §6 的
   「scope 定位 vs trace failing_service」交叉判断（`scope_primary` 也是 None）。
2. **冗余取数**：五维证据查了两遍（取证节点一遍、rca 自己又一遍），token 与时延双付。
3. **与 halt 叠加**：`rca → halt` 现在**能中断整条 run**，而这个判据正是由这个
   "看不见证据的 rca"给出的——判对了是运气（它自己查到了负证据），判错了代价很大。

### 目标

两条 workflow 的 `rca` 都补上（`locate` 已是这个形状）：

```yaml
  rca:
    agent: root-cause
    join: all
    required_edges: [logs, trace, metrics, infra, locate, know]
```

注意 `join: all` 后，scope 判 `insufficient` 的路径上四个取数节点是 SKIPPED
→ rca 入边不全 ACTIVE、但 sources 全终态 → **rca 判 SKIPPED**（而不是像现在这样跑一遍），
正好与 `halt` 汇合。这需要重跑两个场景的 E2E 确认，不能只改 YAML 就收工。

### 涉及文件

workflow 的**真源是数据库**（见 CLAUDE.md §6.0），改完要 `PUT /workflows/{wid}`。
仓库侧无文件——这也是它长期没被 review 发现的原因之一。

### ✅ 已修复（2026-09-17 晚）

两条 workflow 都补上了：

```yaml
  rca:
    agent: root-cause
    join: all
    required_edges: [logs, trace, metrics, infra, locate, know]
```

**实测对比**（同一条 scenario2 工单，同一个窗口）：

| | 修复前 | 修复后 |
|---|---|---|
| `rca` 的六个入参 | `logs/trace/metrics/infra/code/scope_primary` **全 None**，只有 `know` | **七个全部有值** |
| `rca` 启动时刻 | 与 `logs/trace/metrics/infra` **同一微秒** | `locate` 结束 **+13ms**（`15:52:00.876` → `15:52:00.890`） |
| `rca` 耗时 | 35s（自己把五维又查了一遍） | **6.1s**（下游冗余取数一并消失） |
| rca 结论 | 症状/根因对齐靠它自己再查 | 交叉判断**真的发生了**：`code` 入参里是 warrant-service，summary 明写「order-service 的超时只是症状，非根因」 |

scenario1 同样验证通过（`locate` 结束 +23ms 后 rca 启动，七个入参全部有值）。

`join: all` 对 scope 判 `insufficient` 的路径无副作用：那时四个取数节点是 SKIPPED，
rca 入边不全 ACTIVE 但 sources 全终态 → rca 判 **SKIPPED**，与 halt 汇合（实测 halt 那次两图都正常）。

### 顺带修掉：`rca` 的 `summary` 恒缺失

同一次排查里发现：`summary` 在 `RootCauseSchema` 里、在 `fix-planner` 的入参契约里
（「含 root_cause_type / confidence / summary」）、在 `scripts/watch_run.py` 的显示里，
**但不在 prompt 的 JSON 模板里** → 模型不输出它 → 两个场景**每一次** run 的 `summary`
都是缺失的（唯一例外是"证据不足"那条，因为该规则的文字里点名要求了它）。

**`schema 里有 / prompt 模板里没有` 是这一类缺陷的统一判据**——加了一行自检脚本：

```python
props - set(re.findall(r'"([a-z_][a-z0-9_]*)"\s*:', prompt))   # 应为空
```

全量跑下来只剩 `triage.correlation_hint`（无消费方：workflow 读的是
`$.inputs.bug_report.correlation_hint.trace_id`，即**工单入参**，不是 triage 的输出），
属无害的 schema 噪音，未动。

补上后实测：`summary` 正常输出，且正是它把交叉判断的结论讲清楚了
（「根因在 warranty-service 的 checkWarranty/queryWarrantyPeriod……order-service 的超时只是症状，非根因」）。
