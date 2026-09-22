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
| 13 | ~~新租户缺「播种」~~ | ✅ 已实施——三张表一起播（workflow + MCP server + agent 绑定），开箱可用；剩「同步动作」1 项尾巴 |
| 14 | 分层：API ↔ Service ↔ Repository | 债，不阻塞。已有一处**重复实现**与一处**反向依赖** |
| 15 | `datasource/` 例外：保留但立规矩 | 例外**合理**；含一个**无鉴权端点**（只读低危） |
| 16 | ~~MCP 工具绑定只有 server 级粒度~~ | **先不做**——首版证据已被推翻；复核只剩 2 处零星越界，**先改 prompt 即可** |
| 17 | ~~`scope` 不输出消歧字段~~ | ✅ 已解决——**真因是 Worker 未重启**，不是 prompt |
| 18 | **改代码不热载**：Worker 只认库内指纹 | 每次改 prompt/schema 都会静默用旧版——已在实测中骗过一次 |
| 19 | ~~`rca` 的 `join: any`~~ | ✅ 已修——**根因节点从来没拿到过取证输出**（五维摘要恒为 None），见下 |
| 20 | ~~缺 `trace_id` 的工单炸整条 run~~ | ✅ 已修——把"证据缺失"表达成了"执行失败"；修完暴露了下游无守卫，一并补 `locate → halt` |
| 21 | ~~`plan → fix` 无人拍板 + `on_reject` 死配置~~ | ✅ 已修——修复计划没有任何决策点；且 queue 模式下中止逻辑压根不生效 |
| 22 | `remediate` 只产计划、不执行 | `ActionExecutor` 全仓没接线；**附租户 namespace 边界也是开的** |
| 23 | **静默错误行为**（4 条，均已核实） | 表面全正常，只有核数据形状才发现是空的/没接线——最费排查时间的一类 |
| 24 | ~~密钥卫生：`Settings` 密钥字段全是裸 `str`~~ | ✅ 已修——六个字段改 `SecretStr` + 6 条守卫测试；顺带修掉一个被误诊的长期红测试。残留见文末 |

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

### ✅ 部分已实施（2026-09-18）

- **默认只绑 loopback**（原为 `0.0.0.0`）。它没有认证，绑 0.0.0.0 会顺着
  `hostNetwork` 直接暴露到**节点网络**——同集群任何能路由到该节点的东西都能
  POST /exec 执行任意命令。loopback 下只有同 Pod 的 worker 够得着，
  本地 `kubectl port-forward`（打进 Pod loopback）不受影响。
  `SBX_HOST` 是显式逃生阀，跨 Pod 访问要自己承担风险。

### ⚠️ 遗留：出口控制**在当前形态下做不到**（需要先做设计分叉）

原目标里的"NetworkPolicy 限定出口"**在 sidecar 形态下无法实现**，两条硬约束：

1. **NetworkPolicy 按 Pod 选，而 sidecar 与 worker 共享网络命名空间**——
   没有写法能"只掐沙箱的网、放行 worker 的网"。
2. 本地 Pod 是 `hostNetwork: true`，而 hostNetwork Pod **在多数 CNI 下不走
   NetworkPolicy**——即使写了策略也是空转。

**分叉（需定）**：

| | 形态 | 出口控制 | 工作区共享 | 代价 |
|---|---|---|---|---|
| **A（当前）** | sidecar + emptyDir | ❌ 做不到 | ✅ 同 Pod 卷 | 最小；靠"沙箱无凭据"兜底 |
| **B** | 沙箱独立 Pod | ✅ 按 Pod 生效 | ❌ 需换 PVC | 多一套生命周期；对应 §9.3 那个一直没实现的 `max_sandboxes` |
| C | sidecar + Pod 内出口代理 | ⚠️ 部分 | ✅ | 代理本身要维护，且绕不过 hostNetwork |

选 A 的兜底逻辑是：沙箱**没有任何凭据**（拿不到 PAT / DB DSN / 模型 key），
够不着任何需要认证的东西；能做的只剩探测内网与外泄仓库内容。这个边界够不够，
取决于租户仓库里有没有本身敏感的代码——**这是你要拍的**。

#### 换成 B 的代价（2026-09-18 核实，**当前建议保持 A**）

**对 agent 的调用方式零差别**——`SandboxClient` 接口、工具签名、fail-closed 语义都不变，
唯一变的是 `AGENTFLOW_SANDBOX_URL` 的值。差别全在"沙箱什么时候可用、能不能看见代码"：

| | sidecar（A） | 独立 Pod（B） |
|---|---|---|
| 工作区共享 | `emptyDir` 够用（同 Pod 天然共享） | **必须 RWX PVC** |
| 冷启动 | 随 Pod 就绪 | `_wait_ready(timeout=180)`，**最长等 3 分钟** + 拉 JDK 镜像 |
| gradle 缓存 | **跨 run 复用**（worker Pod 处理该租户多个 run，卷是 Pod 级） | 每 run 重下 ~130MB 发行版 + 全部依赖 |
| 故障域 | 沙箱崩 = worker 一起崩 | 沙箱崩 worker 还在，但多一套 Pod 生命周期 |

⚠️ **B 现在有条走不通的默认路径**：`sandbox/orchestrator.py:98` 的默认分支是
`empty_dir={}`，而 **`emptyDir` 跨 Pod 不可共享**——独立沙箱 Pod 拿到的是它自己的空目录，
**看不见 worker clone 的仓库**。必须显式传 `workspace_pvc` 才成立。而 RWX 在本地
minikube 上一般没有（只有 hostPath / RWO），RWO **不能被两个节点同时挂** →
worker 与沙箱必须同节点。**本地单节点能跑通，多节点生产集群不成立。**

**不可兼得的一对**：

```
要出口控制（NetworkPolicy 按 Pod 生效）  →  必须独立 Pod
要 gradle 缓存 + 零冷启动              →  必须 sidecar
```

共享 netns 就没法区分两者，这是 A 的固有代价。

**改口的时机**：仓库数涨到几十个，或租户仓库里确实有敏感内容（如带凭据的配置文件）
——那时"外泄仓库内容"成了真威胁，"多等一次 gradle 下载"就不算什么了。
在此之前，把 `hostNetwork: true` 去掉（生产本就不该有，不去掉任何策略都是空转）
比换 B 更划算。

- 仍未做：exec 服务 token 校验（若走 B，它就是主要控制；走 A 时 loopback 已覆盖大部分）

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

## 6. ⭐⭐ 动态编排（编排层）—— **未实施**，设计见 `docs/design-v5.8.md` §5

> 2026-09-11 记录。来源：`design-v5.4.md`（已并入 `docs/design-v5.8.md` §5）。
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

> **前置**：`docs/design-v5.8.md` §6.2 的 **4 项接缝开放问题**（capability 粒度 /
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

- `make lint` 目前有 **68 个**预置 ruff 错误（改动前后不变，非本次引入）。
  ✅ `api/app.py` 已清空（2026-09-22，见下）—— **它此前那 4 个里有一个是 F821
  「未定义名」，会淹没后续真正的 F821**，所以先清这个文件是有价值的。
- `tests/test_sandbox.py` 5 个用例依赖本机 `~/.kube/config`（本机有 kube 时会因
  incluster 配置缺失而失败；CI 无 kube 时被 skip）

> **实测（2026-09-11）**：本机 `pytest tests/` = **6 failed / 281 passed / 4 errors**。
> 4 errors 全为缺可选依赖（`fakeredis` / `kafka`，本机未 `make install`）；6 failed 为
> kube 依赖 + 陈旧用例 + `test_agent_runner` 需真实 DeepSeek key。
> **与本次改动无关**（`git stash` 前后结果完全一致）。
> **后果：回归信号目前不可信**——这是本项最该先清的理由。
>
> ⚠️ **2026-09-18 更正一条误诊**：上面「`test_agent_runner` 需真实 DeepSeek key」
> **是错的**。真因是 `Settings` 带 `validation_alias` 的字段默认只认别名 →
> `Settings(deepseek_api_key="sk-x")` **静默忽略**该 kwarg、读回空串 → 回退 ScriptedJsonModel。
> 加 `populate_by_name=True` 后该用例**已通过**，**不需要真实 key**。详见 §24。
> 教训：把"测试红了"归因成"环境没配"之前，先确认**参数真的传进去了没有**——
> 静默忽略的入参会让"环境缺失"看起来和"代码路径没走到"一模一样。
>
> **当前基线（2026-09-22）**：**0 failed / 503 passed / 0 errors**。✅ 回归信号**已可用**。
> 清掉的 3 条全是**陈旧用例**（不是产品缺陷），见下方「已清」。

### ✅ 已清：3 条陈旧用例（2026-09-22）

**共同成因：测试断言的是「改动之前的事实」，而改动发生时没人回来改测试。**
三条都不是产品缺陷 —— 但**红了很久没人管，导致回归信号整体不可信**（本项存在的理由）。

1. `build_toolkit() got an unexpected keyword argument 'use_mock'` ×2（`test_build_toolkit_includes_l2_tools`
   / `test_build_toolkit_l2_absent_without_executor`）。
   `use_mock` 随批次 3 删除直连实现（数据面改走 MCP）一起消失，用例未同步。
   判据改成「**没有 `sandbox_client` 参数**」——那才是 L2 工具真正的门控。
2. `test_policy_deny_precedence` 断言 `query_logs` → `ALLOW`。
   而 `query_logs` **已不在本地注册表**（同样迁了 MCP）→ 兜底 DENY。
   这条更值得记：**它与 `sandbox/policy.py:35-36` 那段刻意的设计注释直接矛盾** ——
   注释写着「数据源与 CMDB 工具已迁 MCP……不在此处枚举」，测试却在要求它被枚举。

   **顺带暴露一个真实边界**（已写成 `test_policy_denies_data_plane_tools_not_in_local_registry`
   钉住）：`ToolPolicy` **对 MCP 工具一无所知** —— 它只看本地 `TOOL_REGISTRY`。
   将来即便接上运行期，也只管得住本地工具；MCP 侧的放行是另一条路径
   （readOnlyHint + allow_extra）。这层盲区此前没有任何东西记着。

   ⚠️ 与 §23.3 合起来看：`ToolPolicy` **至今零运行期消费方**，所以这几条断言
   **全绿也不代表租户 deny 规则生效了**。用例的 docstring 已显式写明这一点 ——
   让一个测试在死代码上通过而不标注，比它红着更危险。

### ✅ 已清：`api/app.py` 的 4 个 lint（2026-09-22）

其中**只有 1 个是真缺陷**，列出来是因为它示范了「lint 数不该拿来当"反正是风格问题"」：

- **`F821 Undefined name 'Worker'`（真）**：`worker: Worker | None = None` 指向一个
  **本文件从未 import 过的类型**，而真正赋进去的是 `WorkerPool`。
  运行期不报错是因为文件头有 `from __future__ import annotations`（注解是惰性字符串）。
  危害有两条，都不是"风格"：① 任何 `typing.get_type_hints()` 走到这里就炸；
  ② **读的人会以为存的是单个 Worker**。
  已用 `eval(注解, vars(module))` 复现旧注解 → `NameError: name 'Worker' is not defined`。
- **`PLW0602`（真，但无害）**：`_service()` 里的 `global service` 是**死的** ——
  该函数只读 `service`、从不赋值，而读模块全局本就不需要声明。
  它误导读者以为这里会写全局（真正赋值的是 `init()`）。
- **2 个 `F401`（纯风格）**：`TICKET_NEW` / `TicketStore` 导入后未使用。已确认
  没有别处 `from api.app import` 这两个名字（re-export 假设不成立）才删。

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

## 13. ~~新租户初始化缺「播种」~~ ✅ 已实施（2026-09-18）—— 剩下 2 项尾巴

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

### ✅ 已实施（2026-09-18）：三张表一起播，新租户开箱可用

**做成的事**（用户目标原话："初始化 tenant 操作完成后，就要最小能力可用…开箱即用"）：
`agentflow/seed/` 种子目录 + 在 `TenantStoresRouter._build()` 里播种，
新租户建库即拿到 **workflow + MCP server 注册 + agent 绑定**三样。

| | 内容 | 载体 |
|---|---|---|
| workflow | 两条（与 `agentflow-otr` 库内**逐字节相同**） | `seed/workflows/*.yaml` + `_manifest.yaml` |
| MCP server | 1 个（`aiops-datasource`），**URL 由 `settings.mcp_datasource_url` 注入**（环境相关，不写死） | `seed/dataplane.yaml` |
| agent 绑定 | 7 条（`service-scoper`/`log-analyst`/`trace-analyst`/`metrics-analyst`/`infra-locator`/`code-locator`/`root-cause`） | `seed/dataplane.yaml` |

**为什么"只播 workflow"不够**——后果分三层，一层比一层隐蔽：
① `workflows` 空 → `POST /tickets/{tid}/run` 直接 400；
② `mcp_servers` 空 → agent 绑定不到 server；
③ `agent_configs.mcp_server_ids` 为 NULL → **每个 agent 零工具**，run 会跑完但每个节点
都在"无证据推理"——**看着成功，实则空转**。

**几个实现要点**：
- **接线点选 `router._build()`**（所有建库入口的唯一咽喉：provision / migrate / API 启动 /
  Worker 装配 / 请求路径…）。`_build()` 本来就是 ensure 语义，且将来多一个入口也不会漏。
- **`insert_if_absent` + `ON CONFLICT DO NOTHING`**：`router.get()` 缓存未命中时**无并发保护**
  （两个并发首请求会各 `_build` 一遍），"空表检查"只挡得住单进程；没有 ON CONFLICT 就是
  PK 冲突冒成 500。
- **绑定按名读回真实 server id**，不能用假想的 `seed-<name>`：`mcp_servers` 的唯一约束在
  `name`，租户若已有同名 server，种子那条会被 `ON CONFLICT` 吞掉——此时若还按假想 id 写绑定，
  就绑到**不存在的 server**，症状是静默零工具。
- **`tools` / `enable_tools` / `disable_tools` 不进种子**：那几列是 MCP server load 时
  **运行时发现**的结果，写进种子等于把一次性的发现冻成声明。
- **agent 的 role/stage 不进种子**：从 `agents/agent_config.py` 的静态表取（也是"该 agent 是不是
  内置的"的校验）；`system_prompt`/`schema` 留 NULL 走静态回退，避免与代码构成双真源。
- **`triage` 有意不播**：它不绑工具。已核实 otr 库里那条的 `system_prompt` 与代码**逐字节相同**
  （冗余物化），而"无 DB 行"的静态回退同样是空集 ⇒ 不播它行为完全一致。
- **`seed/` 不 import `api/`**：三个 store 由调用方传入（鸭子类型）——不加重 §14 的分层债。
- **fail-soft 是硬要求**：本模块跑在请求路径上，任何异常都不能外抛（否则新租户首个请求 500）。
- 打包：`pyproject.toml` 加了 `[tool.setuptools.package-data]`。**`-e .` 会侥幸不丢、
  非 editable 安装会静默丢光**——已用 `pip wheel` + `unzip -l` 验证 6 个资源都在。

**实测**（真实 PG + 真实 provision）：
```
[tenantctl] ✅ provision demo-seed: ... workflows=2 servers=1 agents=7
```
幂等重放（含 `--force`）后计数不变；`GET /workflows`（`X-Tenant-ID: demo-seed`）返回 2 条；
`POST /tickets/{tid}/run` **不指定 workflow 也能发起**（原先这里 400）；
`agentflow-otr` 的 2/1/8 行与原始 id **一字未动**。

**为什么选"仓库 seed 目录"而不是 (b) 参考租户复制**（原本倾向 (b)，此处正式否掉）：
(b) 的"参考租户"是**可变状态**——谁改了它、或 `deprovision --confirm-delete` 删了库，
所有未来新租户拿到的默认就跟着变，而且**没有 diff 面、没有 review 入口、没有版本可追溯**。
种子文件能进 PR review、能 `git log`、能离线校验——这比"多一个载体"的代价更值。
防误读靠：`seed-` 前缀 id（`save()` 产出 12 位 hex，永不撞）、manifest 头注释、
`seed/README.md`、`__init__.py` docstring、CLAUDE.md §6.0 —— 五处。

### 还没做（本项尾巴）

1. ~~`provision` 时播种 workflow~~ —— ✅ 见上（播种源已定：仓库 seed 目录）
2. **只剩"数据面绑定"之外的：** ← 见下一节，已随本次一起做了
3. **补一个「同步」动作**：改完 workflow 后一条命令推给目标租户，而不是手工 `PUT`。
   **这条仍然待办**——种子只解决"新租户初始"，**已存在租户的 workflow 更新仍需手工 `PUT`**
   （这正是"改 seed 对已存在租户无效"的另一面）。

<details><summary>原始记录（选播种源之前的讨论，保留备查）</summary>

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

</details>

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

## 20. ~~无 `trace_id` 的工单会整条 run 失败~~ ✅ 已修复（2026-09-18 凌晨）

> 2026-09-17 记录。**修复见文末「✅ 已修复」一节**——注意那里有个反直觉的结论：
> 只让 `locate` 输出负证据**反而更危险**，必须同时补 `locate → halt` 边。
>
> 原始记录：发现路径：改 `rca.join` 后自己造工单做 E2E 验证，
> 一开始忘了带 `correlation_hint.trace_id` → `run_a3d6e9cf55` 直接 failed。

### 现象

`run_a3d6e9cf55`（场景 1，工单**没带** `trace_id`）：

```
triage done · know done · scope done · trace done · logs done · metrics done · infra done
locate **failed**      ← 其余全部正常
→ run failed（locate 声明了 on_failure: abort）
```

`locate` 的失败信息：

```
节点 locate 执行失败（重试耗尽）: agent code-locator 未输出合法 JSON（§7 输出契约未满足）:
'Executed maximum iterations of reasoning-acting loop without finishing the task.'
```

### 成因链

```
工单 correlation_hint.trace_id 缺失
  → trace-analyst 无链路可分析，如实报 found=false（**这个行为是对的**，它没编 trace_id）
  → locate 的 target_service = $.nodes.trace.output.failing_service = null
  → code-locator 拿不到目标服务，只能自己翻来覆去地找 → 迭代耗尽 → 输出契约不满足
  → on_failure: abort → 整条 run failed
```

### 为什么这是个问题

**它把"证据缺失"表达成了"执行失败"**，而这两者在这套设计里是**明确区分**的：
其余四个取数节点遇到负证据都是 `found=false` + 如实说明，然后照常往下走；
`halt` 更是专门为"证据不足"设的出口。唯独 `locate` 会**炸掉整条 run**。

表现在运维侧就是：一个信息不全的工单（真实世界里很常见）不会得到"缺什么"的中断结论，
而是得到一个红色 failed ——**恰恰是 halt 想消灭的那种含混**。

### 目标（二选一）

1. **`code-locator` 学 `trace-analyst`**：`target_service` 为空时直接输出负证据
   （`found=false` + "缺 failing_service，无法定位仓库"）并正常返回，与其余取证节点同形。
2. **`locate` 的 `on_failure` 改 `continue`**：失败时不再 abort。
   ——但这只是把"炸"换成"带个空洞往下走"，不如方案 1 诚实。

倾向 1。`on_failure: abort` 本身有理由（定位错仓库会让 `fix` 改错代码），
**但那该由"输出负证据 → 下游无输入"来兜，而不是靠 abort。**

### 顺带：这是操作手册的坑

场景 1 / 场景 2 的工单**都必须带** `correlation_hint.trace_id`，否则就会踩到上面这条。
已补进 `docs/E2E_VERIFICATION_zh-CN.md` §9.1。

### ✅ 已修复（2026-09-17 晚 / 09-18 凌晨）

两处改动，缺一不可：

**① `code-locator` 输出负证据**（`CodeLocationSchema` + prompt）

它原先的 schema 是五个取证 schema 里**唯一没有 `found`** 的，而 `required` 却是
`[service, repo_url, suspicious_files]`——**结构上不允许说"我没找到"**。于是目标服务缺失时
模型只能反复尝试或硬编一个仓库 → 迭代耗尽 → abort。
（顺带：prompt 里那句"如实上报，不要编造仓库"因此一直没有落点。）

改：加 `found` + `missing`，`required` 收窄为 `[found, summary]`，prompt 加第 4 条
「`target_service` 为空时不要自己找」。

**② 补 `locate → halt` 边**——**这一步是必须的，否则①反而更危险**

实测 `run_c9eb2fe68d`：只做①之后，run **不再失败**了，但——

```
locate 输出 found: false, service: ""（明说"没有定位目标"）
  → rca/plan 照常跑
  → fix 的入参 service = ''（空串）、repo_url = None
  → fix **从 plan 的文字里自己挑了 order-service**，改了 QuotationService.java
```

**改对了是运气。** `ws_*` 工具只校验"这个 service 备过工作区没有"，而工作区是
**默认全量准备**的（三个仓库都在），所以猜错会**静默写进错的仓库**。

**这是①单独上线后的实际效果：把"响亮的失败"换成了"静默地走下去"。**
补上 `- { from: locate, to: halt, when: "$.nodes.locate.output.found == false" }` 才闭环。

### 实测（两个方向都验，`2026-09-18`）

| | 负路径（无 trace_id）`run_8a94d3a979` | 正路径（带 trace_id）`run_b156f65142` |
|---|---|---|
| `locate` | `found: false` + 3 条 `missing` | `found: true` / `service=order-service` / `suspicious_files` 齐全 |
| `halt` | **触发**，`triggered_by: ['locate']` | **SKIPPED** |
| 下游 | plan/rca/fix/test/review **全 SKIPPED** | 正常走到 approve-commit |
| `fix` 的 `service` 入参 | —（未执行） | `order-service`（非空） |

halt 的 `reason` 正是要的那句：「入参 target_service 为空，本节点无定位目标，**不猜测服务名
（猜错仓库会导致下游改错代码）**」。

> **正路径必须一起验**：①②都动了 `locate` 的 schema/prompt，只跑负路径无法排除
> "正路径也被改坏"。上表右列就是这条回归。

### 残留（未修，小）

`locate` 的 `missing` 里有一条「故障时间窗口（无发生时间）」——**但工单其实给了**。
原因是 `locate` 的 params 只有 `{bug, target_service}`，**根本收不到时间窗**，
模型于是把它当成"工单没提供"。措辞不准，但结论（取不到）是对的。
要修就是给 `locate` 补 `start_time`/`end_time` 入参——影响很小，未做。

## 21. ~~`plan → fix` 直连（修复计划无人拍板）+ `on_reject` 是死配置~~ ✅ 已修复（2026-09-18）

> 用户报的：「plan 的结果还没有等待审批通过就走了后续 fix」。查下来是**两条独立的缺陷**
> 叠在一起，都在同一条路径上。

### 缺陷 ①：修复计划没有任何决策点

`plan → fix` 是**无条件直连**，而代码路上唯一的审批 `approve-commit` 在
`fix → test → review` **之后**、且只 gate `commit`。于是"修复计划"这个人本该拍板的东西，
流程里没有它的位置。

**实测代价**（`run_b156f65142`）——plan 自己就写着：

> 第 3、4 步落地前**必须先核对**代码中清理分支与实际生效配置，**否则可能修错位置**

而第 3、4 步正是 `code_fix`。日志显示它们已经跑完了：

```
00:41:50  done plan
00:41:50  ⭐ approval approve-remediate -> waiting_approval
00:42:38  done fix          ← 审批还挂着，代码已经改了
00:42:58  done test
00:43:09  done review
00:43:09  ⭐ approval approve-commit -> waiting_approval   ← 同时挂起两个审批
```

**同一时刻挂两个审批**：人看到的第一个是"审批基础设施止血动作"，而代码那条路早已走完。

> 图作者未必是无意的——`approve-remediate` 的 `name` 写明「审批基础设施**止血动作**」、
> 注释「止血要动生产环境，前置审批」，审批点确实放在"离开沙箱"的动作之前
> （发 PR、动 K8s）。**但 v5.6 §4.6.2 的风险表写的是**
> 「沙箱内代码修复 + 测试」= **medium** → 「注入 approval 节点」——
> 按设计，fix 前面**应该**有审批。

### 缺陷 ②：`on_reject` 从来没人读

两个审批节点都写了 `on_reject: abort`，但全仓消费方为零：

```
$ grep -rn "on_reject" --include="*.py" . | grep -v .venv
agentflow/core/dag.py:71    on_reject: str = "abort"   # 解析进 Node 模型
agentflow/core/dag.py:135   on_reject = spec.pop(...)
agentflow/core/dag.py:159   on_reject=on_reject,
                            ↑ 到此为止
```

实际走向由 `when: approved == false` 边决定。而 `approve-remediate` **同时**写了
`on_reject: abort` 与 `approved == false → recap` 边——**两条矛盾的意图，窄的那条静默胜出**。

`on_reject` 从 design-v5.0 的 YAML 示例就有，v5.1 注释里有「走 on_reject 逻辑」，
`docs/AGENTFLOW_UI_INTEGRATION_RESEARCH` 还把它列为**节点字段**。
**设计里有、实现里没有、测试里零覆盖。**

### 修复

**① 图**（两图都改）：在 `plan` 之后插入 `approve-plan` 审批节点，
`plan **不**直达 fix`；场景 1 的止血路也改挂到 `approve-plan` 之后：

```yaml
  - { from: plan, to: approve-plan }
  - { from: approve-plan, to: fix, when: "$.nodes.approve-plan.output.approved == true" }
  - { from: approve-plan, to: approve-remediate, when: "...approved == true" }   # 场景1
```

**② `on_reject` 落地**（`rejected_abort_node()`）：`abort` = 驳回中止整条 run；
`continue` = 沿拒绝边路由。取值照 design-v5.0 §7.3.2 状态机的
「rejected → 终止/失败」与 `# 或 "continue"`。

**判据必须是状态、不能是动作**——这是实现时踩的第二个坑：第一版写在 `approve()` 里
（驳回时 append 到 `self.failed`），**inline 模式能中止，queue 模式不能**。
Worker 是 `from_checkpoint` 重建 executor 后直接 `run()` 的，**压根不调 `approve()`**
（API 侧已 CAS 落库，恢复时该节点就是 REJECTED）。实测现象：驳回后 run 照样报 `done`，
只有下游被 SKIPPED——**看着像"正常结束"**。改成按状态判定后两条路径同构。

**③ 三处 `WorkflowNodeFailed` 措辞**：默认那句「执行失败（重试耗尽）」对驳回不成立
（驳回压根没重试过），照搬会输出「执行失败（重试耗尽）: 审批被驳回」这种自相矛盾的行。

### 实测

| 场景 | 结果 |
|---|---|
| 跑到 plan 完成 | 挂 `approve-plan`，**`fix` 未执行**（改前：48s 后 fix 就跑完了） |
| 批准 | `fix → test → review → approve-commit` 依次执行（两图都验） |
| 驳回 | `status=failed`，`approve-plan=rejected`，下游全 `skipped`，错误信息写明"这不是执行出错，是人工决策" |

测试 +3（含一条**恢复路径**的回归——正是刚才骗过我的那条），全量 376 通过。

### 残留

- `approve-remediate` / `approve-commit` 仍用 `on_reject: continue`（它们有显式 recap 边）。
  要不要改成 `abort` 是产品选择，未动。
- **超时**（`REJECTED_CANCELED`）不走 `on_reject`——那是 `on_timeout` 的语义，尚未实现。


## 22. `remediate` 分支「只产计划、不执行」——ActionExecutor 全仓没接线

> 2026-09-18 记录，**未修**。发现路径：给租户播种挑默认 workflow 时，核对
> `scenario1` 独有的那条分支到底干什么（见 §13 的播种实施）。

### 现象

`scenario1` 的 `remediate` 节点（agent `infra-remediator`，前置审批 `approve-remediate`）
设计上通过 **ActionExecutor 白名单**执行 `scale_deployment` / `restart_pod` /
`patch_resources`。实测（`run_be23f3b257`，唯一一次它真的跑的 run）：

```
可用工具: []          ← 空列表
工具调用次数: 0
产出: {"changes": [{"action": "restart_pod", ...}, {"action": "patch_resources", ...}]}
```

**产出了一份没人执行的动作清单。**

### 成因

`action_executor` 全仓**只有管道、没有接线**：

```
agents/mcp.py:52      build_toolkit(..., action_executor=None, ...)      ← 有参数
agents/mcp.py:74      build_l2_tools(..., action_executor=action_executor)
agents/tools.py:150   elif spec.name in ("scale_deployment","restart_pod",...) \
                          and action_executor is not None:                ← 为 None 就不建这三个工具
```

**没有任何调用方传过这个值**（`worker.py` / `agents/scopes.py` 零命中）。

### 影响

- 新租户拿到的默认 workflow 里，这条分支**看着有、实际不干活**——播种时已在
  `seed/workflows/_manifest.yaml` 的 note 里注明，免得下一个人以为它能止血。
- 对比：**代码路是真执行的**（`fix-implementer` 的 `ws_write_file`/`ws_git` 确实改了工作区，
  `tester` 真跑了测试）。只有 K8s 这条路"计划即终点"。

### 目标

把 `ActionExecutor` 构出来并传进 `build_toolkit`（`sandbox/action_executor.py` 已实现，
动作受白名单约束）。注意与沙箱的 `SandboxClient` 是同一条 L2 链路的两个分支，接线时要一起看。

**接线时必看（2026-09-18 补记）**：`ActionExecutor.execute(..., tenant_id=None)`
里 tenant 尺度是**开着**的——`_check_ns` 第一句就是 `if tenant_id is not None`
（`sandbox/action_executor.py:101`），而 `agents/tools.py:126` 的 `_l2_action`
**只传 namespace、不传 tenant_id** → §8 P3 的「租户只能操作自己 namespace」这条边界
**从来没生效过**。接线时必须把 `exec_context.current_tenant` 一路透传进去，
否则白名单只剩 `namespace_whitelist` 的静态部分。

---

## 23. 静默错误行为（已核实、未修）

> 2026-09-18 记录，**均未修**。这一批的共同点：**表面上一切正常**——run 报 `done`、
> 接口回 `ok: true`、节点有输出，**只有去核数据形状才发现字段是空的/能力没接线**。
> 前两条是数据契约缺陷，后两条是"整段能力有管道、无接线"。

### 23.1 `GET /runs/{id}` 的 `pending_approvals[].trigger` 恒为 `null`

```python
api/app.py:918   - ``pending_approvals``：[{node_id, trigger, upstream}]，upstream 取上游节点输出
```

`trigger` 是接口**承诺返回**的字段，但实测 **30 条审批记录的 `params` 里无一含 `trigger`**
→ 该入参全是 `null`。前端拿它区分"是哪条边触发了这个审批门"，拿不到就只能猜。
与 §21 的 `on_reject` 同族（都是审批门的元数据没落进 params）。

### 23.2 resume 对终态 run 回 `ok: true`，实际是 no-op

```
POST /runs/{id}/resume  →  {"ok": true, "status": "resumed"}      ← 接口说"已恢复"
Worker 日志             →  [run_xxx] run 已终态，忽略 resume      ← 实际什么都没做
```

`worker.py` 拿 `TERMINAL` 拦下（`design-v5.8.md` §4.7 补记里有完整复现）。
**接口回成功而实际没做**是最坏的一类返回：调用方据此以为恢复了，继续等一个永不到来的状态。
修法二选一：回 `409` + 说明，或回 `{"ok": true, "status": "noop"}` 并让前端显式提示。

### 23.3 `ToolPolicy` 整个类运行期**零消费方** → 租户 deny 规则从未生效

`sandbox/policy.py` 的 `ToolPolicy`（§9.5 租户级工具策略 + §10.2 资源限制）**没有任何
运行期调用方**——全仓仅在 docstring 里被提到。真实生效的是另一条路径：
`agents/scopes.py:26 build_permission_context()`，它**只从 tool registry 生成 allow 规则，
完全不读 `ToolPolicy`**。

后果：`agents/scopes.py:38` 那句注释「叠加租户 deny 规则（M5 接入 tenant 配置）后取交集」
是**未兑现的承诺**——**租户 deny 规则从来没有生效过**。
（相邻小问题：`runner.py:175` 调 `build_permission_context(agent, allow_extra=...)`
**没传 `tenant_id`** → 一律落到默认 `"local"`。）

### 23.4 `SandboxClient` 未接入真实 run

`SandboxClient` 已可用，但 runner 在真实诊断链路中尚未调用
（详见 §9「真实 node_runner 接入 executor」那行；与 §22 是同一条 L2 链路的两个分支）。
现状证据：`agents/runner.py:176` 是 `build_toolkit(agent, mcp_clients=clients)`——
`sandbox_client` / `action_executor` **两个参数都没传**，于是
`agents/tools.py:150` 的分支判断恒为假，L2 工具**根本不会被建出来**。

### 23.5 ~~`ws_git` 有两个**只校验、不生效**的参数~~ ✅ 已修（2026-09-22）

> **这条从"看着在把关"升级成了"把整条 run 挂死"** —— 详见下面「后果」。
> 修法：`message` 真正生效（且 `args` 里的 `-m` 也照收），`remote` 删除。

`agents/workspace_tools.py` 的 `ws_git(service, args, message="", remote="origin")`：

```python
if sub == "commit" and not message:
    raise WorkspaceToolError("git commit 需要 message")
...
full = ["git", *args]          # ← message / remote 都没出现在这里
```

- **`message`**：被校验，但**从不参与构造命令**。提交信息实际由 `args` 里的 `-m` 提供
  → 守卫逼调用方**传两遍**，而它看起来像是在把关"提交信息"。
- **`remote`**：完全没被使用（`push` 的目标由 `args` 决定）。

`ToolSpec` 不做参数 schema，所以这两个参数对 LLM **是可见的**，模型会照着签名去填。
与 §23 其余几条同族：**看着在把关，实际没把关**。

#### 后果：`commit` 节点永久卡在 `running`（run_63a334c90d）

模型照签名填了 `message=`、**没在 args 里带 `-m`** → 真跑的是裸 `git commit` → git 拉起
编辑器（`GIT_EDITOR`/`core.editor` 都没设 → `vi`）→ **`vi` 继承了终端 stdin，永远等键盘**：

```
uvicorn --reload → git -c core.hooksPath=/dev/null commit → vi …/.git/COMMIT_EDITMSG   （7 分 32 秒，一直挂着）
```

`ws_git` 的 `communicate()` 没有超时（`ToolSpec(…, timeout=120)` 是空转的，见 §23.6），
于是节点没有流水、没有报错、run 行停在 `waiting_approval` —— 就是 §28 那个僵尸形态，
**但成因在工具自己身上**。判据：**只要一个子进程继承终端 stdin 且没有超时，就是一条
永久挂起路径**，而挂起比报错难查得多（没有任何现场）。

#### 修法（已实施）

四道保险，缺一不可（`_git_argv` + `ws_git`）：

1. `message` 真的接进命令（args 里已带 `-m`/`--message`/`-F` 就不重复加）；`remote` 删掉。
2. `-c core.editor=true` → 要编辑器的路径**立刻以空信息失败**，不拉 `vi`。
3. `stdin=DEVNULL` + `GIT_TERMINAL_PROMPT=0` + `GIT_PAGER=cat` → **没有可读的终端**，
   交互式子命令只能失败（`git push` 缺凭证同样会停在用户名提示上，这条一并堵住）。
4. `_GIT_TIMEOUT_SEC=120` 超时 → kill + 抛错：**把"挂死"换成"响亮地失败"**。

测试：`test_ws_git_commit_uses_the_message_parameter`（只传 `message=`，用 `wait_for` 兜住
回归形态）、`test_git_argv_always_disables_hooks_and_editor`、`-F -` 走 stdin 的响亮失败。

#### 同一套保险也补给了 `_run`（`ws_open_pr` 的执行器，2026-09-22 同日）

`ws_open_pr` 内部跑命令用的是另一个辅助函数 `_run`（`git push`、`gh pr list/create`），
它原先**也是**继承 stdin + 无超时 —— 同一个挂起形态，只是触发条件是**缺凭证**：
`git push` over https 会去终端问用户名（实测本机 `credential.helper` 为空），`gh` 也会
问"选哪个仓"。而这条路径恰恰是**第一次真跑 GitHub 远端时**才走到。现在两处共用
`_subprocess_env()`（`GIT_TERMINAL_PROMPT=0` / `GIT_ASKPASS=true` / `GIT_PAGER=cat` /
`GH_PROMPT_DISABLED=1`）+ `stdin=DEVNULL` + `_SUBPROC_TIMEOUT_SEC`。

> 共用一份 env 是刻意的：两个消费方各写一份，漂移的那一半不会有任何提示 ——
> 本仓为"两份实现"付过代价（`_mark_cancelled`，CLAUDE.md §11）。

测试：`test_run_disables_all_interactive_prompts`（变异：删掉 env → 红）、
`test_run_times_out_instead_of_hanging`（变异：删掉超时 → 30 秒后红）、
`test_run_gives_subprocess_no_terminal`（只锁契约，**抓不住漏传 DEVNULL** —— 已注明）。

### 23.6 `ToolSpec.timeout` **全仓零消费方** —— 声明了 120 秒，实际没有超时

```python
class ToolSpec:            # agents/tools.py:15
    timeout: int = 30      # ← 没有任何地方读它
```

`grep -rn "spec.timeout"` 全仓零命中；`build_toolkit` 注册时也不传（`Toolkit(tools=…, mcps=…)`）。
`level` / `needs_approval` 有消费方（API/UI 要用），`timeout` / `rate_limit` 没有 ——
**与 §23.3 的 `ToolPolicy` 同族**：声明在那儿，看着像有约束，实际没有。

§23.5 那个挂死能被"声明的 120 秒"拦住吗？不能 —— 拦住的必须是**代码里真的在等的那个超时**
（`_GIT_TIMEOUT_SEC`）。**未修**（要么在注册处真正接线，要么把字段删掉别摆着）。

---

## 24. ~~密钥卫生：`Settings` 全部用 `str` 存，`repr()` 明文带出~~ ✅ 已修（2026-09-18）

> 发现路径：给 `deploy/worker-deployment.yaml` 加 DeepSeek key 时，回头核
> "这个 key 会不会被写进 git / 日志"。**Settings 部分已修**，尾巴见文末"残留"。

### 原缺陷（源码事实，未读取任何真实值）

`agentflow/config.py` 里六个字段是裸 `str`，且**全文件 `SecretStr` 出现 0 次**：
`deepseek_api_key` / `jwt_secret` / `secret_key` / `open_sandbox_api_key` /
`langfuse_secret_key` / `postgres_dsn`（连接串含明文口令）。

`str` 是 pydantic 的默认显示类型 → **`repr(Settings)` / `str(Settings)` 原样打印全部密钥**。

**为什么不是"理论风险"**：pytest / CI 的失败回溯**默认打印局部变量**，本仓已出现过
`settings = Settings(deepseek_api_key='...')` 这种输出形态。本地那次是 monkeypatch 的假 key，
**CI 里注入的是真 key，同一形态就会把真 key 打进构建日志**。另两条路径：任何
`log.info("... %s", settings)`；接了 Sentry 一类错误上报后自动采集局部变量。

### ✅ 已修

1. 六个字段改 `pydantic.SecretStr`；取明文处显式 `.get_secret_value()`
   （`scopes.py` ×2、`auth.py` ×1、`management_store.py` ×2、`postgres_dsn()` helper ×1）。
   未加 `.get_secret_value()` 的只剩**真值判断**（`if not settings.jwt_secret`）——
   `bool(SecretStr(""))` 为 `False`，与裸 str 一致，已用测试钉住。
2. 新增 `tests/test_config_secrets.py`（6 条）：字段必须是 SecretStr、
   `repr`/`str`/`model_dump_json` 不含明文、**校验错误信息**也不含明文、
   明文仍可取出、真值判断与裸 str 一致、赋值走校验。
   全部用哨兵值，不读也不断言任何真实凭证。

### ⚠️ 顺带修掉一个**被误诊**的长期失败测试

`test_build_reasoning_model_thinking_enabled_with_key` 一直红，`§10` 把它记成
「需真实 DeepSeek key」。**真因是别名陷阱**：带 `validation_alias` 的字段默认只认别名，
于是 `Settings(deepseek_api_key="sk-x")` **静默忽略**这个 kwarg、读回空串 →
一路回退 `ScriptedJsonModel`。与 §23 是同一族（**不报错、行为静默走错**）。
修法：`model_config` 加 `populate_by_name=True`（环境变量仍走别名）+ `validate_assignment=True`
（否则 `monkeypatch.setattr(s, "jwt_secret", "x")` 塞进裸 str，`.get_secret_value()` 报 AttributeError）。
**该测试现已通过**，§10 的"需真实 key"描述作废。

### ✅ 顺带修掉：`tenantctl` 五处回显解密后的 db_ref DSN

DSN 含明文口令，打进终端回滚 / CI 日志 / 截图就收不回来；`CLAUDE.md` 早有
「任何 API 不回显 DSN」，CLI 只是没被那条规则覆盖。改为统一经
`tenants.mask_dsn()`：**只打口令，host/port/库名/用户名原样保留**
（排查要用），两种凭据位置（netloc 与 query）都覆盖。实测输出：

```
[tenantctl] 🆕 已创建租户库 postgresql://agentflow:***@10.89.0.2:5432/agentflow-team-x
```

守卫方式不是靠人眼——`tests/test_dsn_masking.py` 用 **AST 扫描** tenantctl 的每个
`print(...)`：凡取出 `'dsn'` 值而未过 `mask_dsn` 的即报错（含一条**自检**，
确保守卫本身在重构后不会静默失效）。

### 残留（未修）

- **K8s 侧（非本仓代码）**：`kubectl create secret --from-literal=...` 把明文放进
  **进程 argv**（`ps` 可见），改用 `--from-env-file` / stdin；Secret 默认**只是 base64**，
  `kubectl get secret -o yaml` 即可读，etcd 未配 encryption-at-rest 时落盘也是明文
  —— 生产需 sealed-secret / 外部 secrets manager。
  （注：`deploy/worker-deployment.yaml` 注释里写的是 `$AGENTFLOW_DEEPSEEK_API_KEY`
  **变量引用**形式，shell 历史记的是未展开文本，不会因它泄 key。）

---

## 25. `ws_git` 白名单**允许 `push`**：「建 commit」与「推远端」之间没有权限边界，只有提示词

> ⚠️ **2026-09-21 前提变动**：本节是因 `problem-log-diagnose` 的 `commit` 节点才写的，
> 而那条流程的修复段**已整体删除**（它现在只到「诊断输出门」为止，不写代码）。
> **但本节仍然有效、优先级不变**——`ws_git` 是平台内置工具，白名单与提示词都没变，
> 而**另外两条流程**（`agentflow/seed/workflows/` 的 scenario1/scenario2）**仍然有**
> `fix → test → review → approve-commit → commit` 段，`committer` 仍然会被调到。
> 也就是说：**在这里删掉的本节内容，保护不了那两条流程**。

> 2026-09-20 发现。路径：给 `problem-log-diagnose` 补修复段（`fix → test → review →
> approve-commit → commit`）时，要写清 `commit` 节点到底做了什么，于是去核
> `committer` 的提示词与 `ws_git` 的白名单——**两处对不上**。

### 源码事实

`agents/workspace_tools.py:37`：

```python
_GIT_ALLOWED = {"status", "diff", "add", "commit", "push", "rev-parse", "branch", "checkout", "log"}
```

`push` **在白名单里**。白名单只禁 `pull`/`fetch`/`reset`（§4.6 版本冻结——防 HEAD 漂移），
与"能否写远端"是两件事。

而 `committer` 的提示词（`agents/prompts.py`）只让它调
`ws_git(service, ['add'|'commit'|'rev-parse', ...])`，并在规则里写
「分支已由工作区准备时建好（`aiops/RUN_<run_id>`）；**不要用 pull/fetch/reset（被白名单拒绝）**」。
——它把「白名单拒绝」当作约束的**唯一理由**来陈述，而 `push` 恰好不在被拒之列。
一个照着"白名单就是边界"去推理的模型，会得出「push 没被禁 ⇒ 可以 push」。

另外确认：平台**没有任何开 PR 的能力**（全仓无 github/gitlab API），
`committer` schema 的 `pr_url`/`pr_number` 是形态占位。

### 为什么不是理论风险

**风险完全取决于 `origin` 指向，而这是部署配置、不是代码**：

- `AGENTFLOW_REPO_ROOT` 指本地 `file://` 源（本仓 `tests/test_workspace.py` 的用法）→ push 只是写本地目录，无害；
- 生产接真实远端（`https://…` 且 PAT 在 URL 里）→ agent 一旦调 `push`，
  **在真实远端建分支**。这是个**对外可见、难以撤回**的动作，而审批人在 `approve-commit`
  卡片上看到的是 diff + 测试 + 审查意见——**没有任何一处告诉他"这一步会推远端"**。

`ws_git` 对 `commit` 强制要 `message`（`WorkspaceToolError("git commit 需要 message")`），
对 `push` 则**无任何前置条件**。

### 与 §23.5 同族

§23.5 记的是 `ws_git(service, args, message="", remote="origin")` 里
**`remote` 参数只校验、从不生效**（push 的目标实际由 `args` 决定）。
本条是它的邻接面：**`push` 这个子命令本身没被当作"需要单独把关的动作"**。
两条合起来的效果是——想控制"这次到底推不推、推到哪"，当前**没有任何落点**。

### 改法（未实施，需评审）

三选一，取决于产品意图：

1. **收紧白名单**：`push` 从 `_GIT_ALLOWED` 移除。若"提交 = 只在本 run 分支内落 commit"，
   这是最诚实的一刀——平台的产物应当是分支 + diff，推送由平台外的流程做。
2. **给 push 单独设门**：保留 `push`，但要求显式 `remote` 白名单（同时修 §23.5 那个死参数），
   并把它做成**独立的审批节点**或至少进 `approve-commit` 卡片要展示的内容。
3. **显式声明为设计边界**：在 `committer` 提示词里写清"可以 push、推到 `origin`，
   即配置的远端"——**消除"白名单即边界"这个错误推理**，风险交由部署方评估。

在定下来之前，原 `problem-log-diagnose` 的 YAML 里曾在 `commit` 节点注释里写明这条
**不是**权限约束——**那段注释已随修复段一起删除**（该流程不再有 `commit` 节点）。
现存的三处说明改由本节的「前提变动」承担：`approve-commit` 的
`scenario1-quotation-print-fail.yaml:242` / `scenario2-bug-fix.yaml:253` 附近仍有对应注释。

---

## 26. APM 侧三个端点没有终态守卫：`escalated`（以及 `resolved`）会被无条件改写

> 2026-09-21 发现（做「升级 = 生成工单」时）。**已知边界，本轮不修**。

`aiops-apm-anomaly-detector` 的 `src/aiops_apm/router/problems.py` 里，**三个端点不检查
problem 的 `state`**：

| 端点 | 行 | 行为 |
|---|---|---|
| `POST /{record_id}/resolve` | `:161` | 无条件 `records.resolve()` → `state=resolved` |
| `POST /{record_id}/ignore` | `:189` | 无条件 `records.close()` → `state=closed` |
| `POST /{record_id}/run-decision` | `:498` | 只追加 evidence，不改 state（危害小） |

存储层的守卫只挡**目标状态**：PG 的 `resolve` 是 `WHERE ... AND state <> 'resolved'`、
`close` 是 `AND state <> 'closed'`（`storage/records.py:363` / `:371`）；memory 版**完全无条件**。
⇒ 一条 `escalated`（已升级、已建工单）的问题单，调 `/resolve` 会被改成 `resolved`。

**为什么本轮不修**：这不是升级引入的新洞——`resolved` 同样能被 `/ignore` 改成 `closed`，
两者是对称的既有形状。而 `decide_problem_diagnosis` 那三处终态守卫
（`problems.py:398` 的 analyze、`:639` 的 diagnose、`:1036` 的 decision）**本轮已加 `escalated`**，
且前端行内的 Ignore 按钮对非 `pending|in_progress` 不渲染（`js/app.js:4301`），
**UI 上走不到**；是 API 层面敞着。

**要修的话，判据是先定语义**：`resolved`（已修复）与 `escalated`（已派单）都是终态，
`/ignore` 该不该能改写 `resolved`？定了这条，三个端点的守卫才有统一写法——
**别只给 `/resolve` 加守卫**，那会制造 API/UI 语义分叉。

---

## 27. `POST /runs/{id}/approve|reject` 打**不存在的节点 id** → HTTP 500（不是 4xx）

> 2026-09-21 发现（做「升级 = 生成工单」时实跑真环境撞到）。**未修**。

### 现象

```bash
curl -s -X POST localhost:8000/runs/$rid/approve -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: otr' -d '{"node_id":"diagnose-output","by":"probe"}'
# → Internal Server Error   HTTP 500
```

节点 id 不存在时 500；节点**存在但不在等待审批**时才是干净的 400
（`approve` 里那句 `assert ... == WAITING_APPROVAL`，`AssertionError` 已被端点映射成 400）。

### 根因

`executor/dag_executor.py` 的 `DAGExecutor.approve` **第一行**：

```python
node = self.dag.nodes[nid]      # ← 节点不存在 → KeyError
assert node.is_approval, ...    # ← 下面两条 assert 才会被映射成 400
```

`KeyError` **不在** `api/app.py` 审批端点的异常映射里（那里只映射
`ValueError/AssertionError → 400`、`ApprovalRaceError → 409`、`ApproverNotAllowed → 403`）
→ 冒到顶层 → 500。

### 为什么要登记而不是当场修

- **UI 走不到**：run 查看器的审批按钮 `data-node` 取自 `pending_approvals[].node_id`
  （`js/app.js` 的 `renderRunApprovals`），永远是图里真实存在的节点。
- 修它会改**对外状态码**（500 → 404/400），而前端各处对审批失败的提示是按既有映射写的，
  属于契约变更，该单独评审。
- 触发它的那条路径（APM 侧猜一个门节点 id）**已在本次一并根治**：`_agentflow_gate_node`
  在没有 `pending_approvals` 时返回 `None`，调用方**跳过那次出站**而不是猜一个 id
  （见 `aiops-apm-anomaly-detector` 的 `_decide_agentflow`）。

### 要修的话

判据是**"节点不存在"该算 404 还是 400**：`approve` 的入参是节点 id，打错 id 更接近
"资源不存在"。定了这条再改映射（或在 `DAGExecutor.approve` 里把 `KeyError` 换成
带节点 id 的 `ValueError`，那样端点现有的映射不用动——**后者更省**）。

---

## 28. `running` 状态的**僵尸 run** 没有自愈路径（Worker 崩溃/被杀后永久卡住）

> 2026-09-21 发现（重启 Worker 前查"有没有正在跑的 run"时撞到）。**未修**。

### 现象

```bash
$ curl -s localhost:8000/runs/run_dfa3801aee -H 'X-Tenant-ID: otr' | jq '{status,created_at,updated_at}'
{ "status": "running",
  "created_at": "2026-09-17T06:33:16",   # 4 天前
  "updated_at": "2026-09-17T06:33:17" }  # 创建后 1 秒，此后再没动过
```

13 个节点里 12 个 done，`review` 卡在 `running`。**它不是"正在跑"，是死了。**

### 根因

Worker 认领 run 走 `cas_update_run_status(queued → running)`，认领后**没有 lease /
没有 heartbeat**。进程一死，那行就永远停在 `running`：

- **不能被 trigger 重新认领** —— CAS 只从 `queued` 转；
- **不能被 resume** —— `worker._cmd_resume` 的 CAS 只接受 `paused` / `waiting_approval`；
- 重启 Worker 也没用（它对 `running` 的 run 视而不见）。

### 为什么这次没修

- 修它要引入 **lease + heartbeat**（或一条"启动时扫描超期的 running run 并回收"的自愈路径），
  那是状态机层面的改动，牵扯 Worker 生命周期与多副本语义，该单独设计与评审。
- 眼下有一个**手工逃生阀**（2026-09-21 救 `run_843dd83d86` 时用过）：
  直接改库把状态挪回一个可认领的值——
  `UPDATE runs SET status='paused' WHERE run_id='…'` 然后 `POST /runs/{id}/resume`。
  ⚠️ **它绕过了状态机**，只应在明确知道那条 run 确实没有活着的 Worker 时使用。

### 判据

**"这条 run 还有没有活着的执行者"** 必须有地方可查（lease 表 / heartbeat 列），
否则运维分不清"在跑"和"死了 4 天"。

## 29. `approvals` 表**没有决策时间列** —— 审批历史给不出"什么时候批的"

> 2026-09-21 发现（做 Run 详情「审批」页签时）。**未修**。

表里有 `timeout_at`（超时闸门的落点），但**没有 `approved_at` / `decided_at`**。
于是 `GET /runs/{id}` 的 `approvals[]` 能给"谁批的、批没批、为什么驳回"，给不出时间。

要补得**加列 + 迁移**（`statestore/sqlite.py` 与 `postgres.py` 两处建表 + 迁移脚本），
并在 `cas_update_approval` 写入时落值。不是难事，但属于 schema 变更，该单独一轮。

> 已写在 `statestore/base.py::get_approvals_for_run` 的 docstring 里，
> 免得下一个人以为是自己漏读了字段。

## 30. 锁竞争超时对外是 **HTTP 500 空响应体**

> 2026-09-21 发现（验证 `AGENTFLOW_LOCK=redis` 是否生效时撞到）。**未修**。

`RunService._acquire_tenant_lock` 抢不到锁时抛 `TimeoutError`，而 `api/app.py` 的
`run_ticket` 只捕获 `TenantQuotaExceeded` 与 `InputsValidationError` → 漏到顶层 → 500。

```
HTTP 500  耗时 10.02s     ← 正是那个 10.0s 轮询超时
"Internal Server Error"   ← 响应体里没有任何原因
```

**但抢不到锁是运行态问题**（redis 抖了 / 有人占着），不是服务端 bug：
500 空响应让运维无从下手（是 redis 挂了？是租户超额？还是代码坏了？）。

### 判据

**"依赖不可用"与"服务端出错"要能从状态码上分开**（503/409 带原因 vs 500）。
定了再改 `run_ticket` 的异常映射。

## 31. `returnApmTicketStatus` 是**单地址**配置 —— 多原系统尚未设计

> 2026-09-21 记。**未修**（当前只有一个原系统，够用）。

回调地址走 `DATASOURCE_APM_TICKET_URL` 这一个部署级配置。若将来不同租户要接各自的
工单平台，需要按租户解析地址——那时"地址是部署属性"这条前提就不成立了，
要么回到调用方传（并解决 13.4 里那两条问题：让模型决定地址、每单重传），
要么让 MCP server 侧持有租户→地址的映射。

**现在不做**，但要把这个前提记下来：它会决定那张配置表长什么样。

## 32. 沙箱链路的两道新拦网**各有边界**（产物守卫 / 往返探针）

> 2026-09-22 记。本次为 run_fc9e158b55 加了两道拦网（`ARTIFACT_FIELDS` 产物守卫、
> `_env_preflight` 的沙箱↔工作区往返探针，见 CLAUDE.md §3.3/§9.6）。它们**故意窄**，
> 边界登记在此 —— 别把"没报"当成"没问题"。

**① 写盘自校验只覆盖 `ws_write_file`。** `ws_write_file_sandboxed` 写完会在 worker 侧
复读（判"两侧是不是同一个卷"）；而 L2 的 `sandbox_write_file` 只到
`SandboxClient.write_file` 为止 —— 它现在会认沙箱回的 `written` 标志（**拒写**不再被读成
成功），但**不做复读**，所以"写成功了、东西落在别处"这个形态经它改代码仍看不出来
（`fix-implementer` 两张表都注册了）。环境层面的错配由 ③ 的探针兜住，但**单次调用**没有
判据。要么把复读下沉到 client 层（client 不知道 worker 的工作区根，得加参数），要么把
`sandbox_write_file` 从 fix 的工具表里去掉。**未做**。

**② 产物守卫只判"一次成功的写都没有"。** "改了一部分"（声称 3 个、只成功了 1 个）不判 ——
逐条核对路径的假阳性面太大（`files_changed` 是模型写的散文，工具入参是另一份字符串；
而 `fix` 是 `on_failure: abort`，一次误判就中止整条 run）。那一层交给下游 `test` 核对工作区。

**③ 往返探针只探工作区根一个路径**，且**在集群外经 port-forward 打 Pod 沙箱时会误报**
（文件在 Pod 的卷里、本机看不到）—— 文案里写了怎么区分。K8s 形态 worker 与沙箱同 Pod
共享卷，天然满足，不受影响。

**④ `WorkspaceManager.__init__` 的默认值 `Path("/tmp/workspace")` 与 `config.py` 的
`/tmp/agentflow-workspace` 不一致**（`workspace/manager.py:90`）。当前所有调用方都显式传
`workspace_root`（`service.py:231` 传 settings、`workspace_tools._workspace_root()` 也传），
所以这个默认值**到不了**运行期 —— 但它是个漂移面：将来有人直接 `WorkspaceManager(t, r)`
就会落到另一个目录，且**没有任何提示**。**未做**（改默认值要连带看三个测试替身）。

**⑤ ~~`reviewer` 的输出会被截断 → `AgentOutputError` → 整条 run 中止~~ ✅ 已修（2026-09-22，见下）**。
实测 `run_74a0db73ae`（2026-09-22，验证本次沙箱修复时跑的）：
`fix`/`test` 都过了（测试真跑、`rc=0`、JUnit `tests=5 failures=0`），而 `review` 判
`执行失败（重试耗尽）: agent reviewer 未输出合法 JSON`，错误文本里那段 JSON 停在半句
（`"已核对 QuotationException 定义（extends`）—— **模型把 `comments` 写得太长、输出被
截断**，JSON 因此不合法。它声明了默认的 `on_failure: abort`，于是 run 就此中止，
`commit`/`ticket-done` 都不执行 —— **工单不回传**。

与 §10/§23 里那条 `code-locator` 的"未输出合法 JSON"**不是同一个成因**（那条是
`Executed maximum iterations of reasoning-acting loop`，轮次耗尽）。这条更像输出长度上限：
该节点 3 次 LLM 调用、`tokens=7000`。

#### ✅ 已修（2026-09-22）：三处，先让它可诊断、再消因、最后降概率

**复发过一次**（`run_3f977237be`，同日 07:50，同一个节点、同一形态）—— 所以不是偶发，
按下面三条一起修了：

1. **`AgentOutputError` 带 头 + 尾 + 总长**（`agents/scopes.py`）。原来只有 `[:200]` 的
   头，而**成因恰恰藏在被丢掉的那一段后面**：「被输出上限截断」与「JSON 里混了未转义
   字符」两种事后完全无法区分 —— 这条不补，后面两条只能靠猜。这一条是**下一步的判据**。
2. **`max_tokens` 显式设**（`config.py:deepseek_max_tokens`，默认 8192；两个模型构造处
   `build_model` / `build_reasoning_model` 都传 `Parameters(max_tokens=…)`）。
   不设就走 provider 默认，长回复被**截在句子中间**。8192 实测该 provider 接受
   （2026-09-22，直接打 `/chat/completions` 验的）。
3. **`reviewer` 的输出约束**（内置提示词）：`comments` 最多 3 条、每条 ≤ 100 字、
   **不要复述 diff** —— 从"输出小一点"这一侧降概率。otr 没有 DB 覆盖行，改内置的就生效
   （有覆盖的租户要在 `agent_configs` 里同步改）。

测试：`test_agent_output_error_keeps_head_and_tail`（变异：退回"只留头" → 红）、
`test_models_send_explicit_max_tokens`（变异：`build_model` 不带 parameters → 红）。
**仍未做**：parse 失败时的"修复轮"（把上次输出回喂给模型让它只补 JSON）—— 那是另一件事，
且要先有 1 的判据才知道值不值得做。

## 33. 工单回传用错了号：**派单号 ≠ 问题单号** ✅ 已修（2026-09-22）

**现象**：`ticket-done` 报 `delivered=false`，工具回
`HTTP 404 {"code":"NOT_FOUND","reason":"没有持有工单 PR-20260922-0001 的问题单"}` ——
修复做完了、PR 也开了，**原系统永远收不到**。

**根因**：一张升级工单上有**两个号**，而下游把它们当成了同一个：

| 号 | 例子 | 出处 | 谁认它 |
|---|---|---|---|
| **派单号** | `INC-20260922-0002` | APM `problems.py:1225`（`next_ticket_number()`）→ 写进 agentflow 工单的 `number`，同时进 APM 记录的 evidence | `records.find_by_ticket`（**只认它**） |
| **问题单号** | `PR-20260922-0001` | APM `problems.py:404`：`_build_ticket(rec)` 用 `rec["record_id"]` → 成了载荷里的 `bug_report.number` | `ticket-done` 的提示词（"ticket_id 原样取自入参 `ticket.number`，如 `INC95528`"）—— 它**要的本来是派单号** |

`find_by_ticket` 只按 evidence 里的 `ticket_number`/`ticket_id`（或 `resolve_reason='escalated:<号>'`）
匹配 → 传问题单号**必然 404**（接收端刻意"不建单、不猜"，见 `storage/records.py:108`）。
实测 `run_a80df3e5d3`：工单 `number=INC-20260922-0002`、载荷 `bug_report.number=PR-20260922-0001`。
**它影响的是所有从这张工单发起的 run**（那条工单的 `run_ids` 躺着 11 条，每条都会红在最后一公里）。

**修法**（agentflow 侧，`api/app.py:_run_inputs_from_ticket`）：run 的**入参副本**里把
`bug_report.number` 对齐成**工单自己的 `number`**（派单号），原问题单号留在
`bug_report.problem_number`（**只在对不上时写**，免得每张单都多一个噪音字段）。
**不动工单记录本身**（详情页仍显示原样）。测试：
`test_run_ticket_aligns_the_dispatched_ticket_number`（变异：撤掉对齐 → 红）。

**没做**：APM 侧 `find_by_ticket` 是否该顺带支持按 `record_id` **精确匹配** —— 那是另一个仓的
设计决定（做了等于"问题单号也能反查到"）。当前只在**发起侧**对齐，回传契约不变。

### 32⑥ ~~postmortem 的 JSON 里带裸换行~~ ✅ 已修（2026-09-22，同日第三例）

**现象**：`run_c2c44f9ff8` 的 `recap` 报 `未输出合法 JSON（共 1964 字符）`。
**这次能一眼定性，靠的正是同一天补的 ①**：错误里带上了**尾部**，而尾部是完整的
`…保持稳定"]}` —— 所以**不是截断**，是 JSON 本身坏在**中间**。

**根因**：`postmortem` 的字段全是长中文散文（`summary` / `root_cause` / `actions[]` /
`followups[]`），模型**在字符串里直接换行** —— RFC 8259 不许裸控制字符，`json.loads` 因此报错。
而它的**意图完全清楚**（那个换行就是字符串内容的一部分），用标准去惩罚一个不存在的歧义
是错的。

**修法**（两处，`agents/scopes.py`）：

1. `extract_json` 用 `json.loads(raw, **strict=False**)` —— 允许字符串里的裸控制字符。
   这是**放宽**，对合法 JSON 零影响（`abort` 类节点不再因为"散文换了行"整条 run 中止）。
2. **错误预览不再折叠空白**。原来 `" ".join(text.split())` 会把"字符串里裸换行"压成空格
   —— 而那正是最常见的写飞形态，压完就再也看不出来了。现在 `repr` 下**裸换行显示 `\n`、
   合法转义显示 `\\n`**，两者一眼可分（诊断结论完全相反）。

测试：`test_extract_json_tolerates_raw_newlines_in_strings`、
`test_agent_output_error_keeps_raw_escapes`（两条都做了变异验证：撤掉 → 红）。

> **同一条 run 的另一半是好消息**：它的 `ticket-done` 是 **done**，带回
> `{"ticket_id": "INC-20260922-0002", "status": "resolved"}` —— APM 那条记录实测已
> `state=resolved`、`reason=agentflow:resolved:INC-20260922-0002`、evidence 多了一条
> `ticket_status/resolved`。**§33 的票号对齐端到端生效**，三仓闭环第一次真正合上。
