# design-v5.7 — CMDB 业务域分层 + 诊断链「定位问题服务」环节

> 状态：**§7.1 与 §7.2 均已实施并 E2E 验证**（逐项状态见各自表格）
> 基线：`design-v5.6.md`（编排层 × 数据面合并版）
> 前置：CMDB 实体图谱化已完成（`aiops-mcp-servers` `af3e88a`，见 `docs/cmdb-entities.md`）
> 日期：2026-09-15 起，2026-09-16 实施完成
>
> **已实施**（两部分）：
> - **CMDB 本体**（`aiops-mcp-servers` `e6c7a34`）：删 `cross_journey_hub`、加 `domain`、
>   边加 `layer`、business 层禁 app—app、`app.kind`、`refs`→`app_codebase` 边、
>   节点加 `description`/`keywords` —— **对外契约零改动**
> - **编排层**（`service-scoper` agent + `scope` 节点 + 四个取数节点改 `join` + `rca` 交叉核对）
>   —— 全链 E2E 跑通至审批节点
>
> **未实施**：① 意图分类只落到了 `scope` 的**输出字段**（`intent`），尚未按它分支路由召回策略；
> ② §3.6 的「信息不足 → 停止让用户补充」——`scope` 会输出 `insufficient` 供下游判断，
> 但**流程不会真的停下来**（`kind: clarification` 节点与 `WAITING_INPUT` 状态都没建）；
> ③ 「召回为空 → 全量给 LLM」的兜底在 agentflow 侧未实现。

---

## 1. 背景与目标

### 1.1 问题

上一轮把 CMDB 落成了实体图谱（12 类节点 / 11 类边 / 文件承载），但**业务语义层是空的**：

- Portfolio 节点是**裸名字**（`{"id": "portfolio:order", "name": "order", "attributes": {}}`），
  而且这 6 个是从 `namespace` 派生的——**k8s 部署分组冒充业务域**
- Journey / Enterprise 节点数为 0
- 结果：**LLM 无法判断"这张工单跟哪些服务相关"**。拿「用户反馈结账卡住」去对 `order`
  这个词，可判断的信息几乎为零

同时诊断链上**没有"定位问题服务"这一环**。服务名的实际来源只有两条，都不可靠：

| 路径 | 机制 | 问题 |
|---|---|---|
| `trace-analyst` 的 `failing_service` | ES 链路日志启发式 | 只在有 trace 时成立；是**测试床特定经验** |
| 各 analyst 从 triage 摘要里猜 | LLM 自由发挥 | `metrics-analyst` 提示词自己在劝"不要反复试不同 service 做开放式探索——会耗尽轮次" |

而工单上其实**带着** `bug_report.cmdb_ci.name`，`create_ticket` 把它落到 ticket 的
`service` 列——**但它只被存储和展示，不参与任何路由**（`_ticket_inputs()` 只把
`bug_report` + 时间窗放进 `inputs`；`cmdb_ci` 在整个 `workflows/` 目录里出现 0 次）。

### 1.2 目标

1. **业务域分层建模**：Enterprise → Journey → Portfolio → App 四级收敛树，让"服务承载什么
   业务"在图上有据可查
2. **业务语义可检索**：业务域节点带 `keywords`（检索键）与 `description`（判断依据），使
   LLM 判断有据可依、并使**确定性召回**成为可能
3. **诊断链新增 `scope` 节点**：在 triage 之后、取数之前，基于 CMDB 定位问题服务，
   输出带置信度的候选集——**取代"靠 trace 启发式 + agent 猜"的现状**
4. **工单自带的 service 作为高权重先验**，可被推翻但需给反证，且**始终扩展一跳**

---

## 2. 本体修订

### 2.1 节点类型（净 12 类：删 1 加 1）

**删除 `cross_journey_hub`**——新模型是收敛树，没有横向枢纽的位置。当前该类型节点数为 0，
删除不影响任何数据。
**新增 `domain`**（业务细域）——见下。

| 层 | 节点类型 | 说明 | 当前数据 |
|---|---|---|---|
| **业务** | `enterprise` | 企业视角的功能：销售 / 售后 / 零售 / 理赔 | 0 |
| | `journey` | 用户旅程：售后保养 / 销售线索管理 / 用户进店 | 0 |
| | `portfolio` | 业务领域：销售库存 / 售后服务选择 / 配件购买 | 6（⚠️ 见 §2.5） |
| | `domain` | **业务细域**——比 portfolio 更细，**与 portfolio 平行而非其子级** | 0 |
| **应用** | `app` | 应用/服务，**或其所在云环境**（见 §2.3） | 10 |
| **支撑** | `team` / `agent` / `tool` | 团队 / 智能体 / 工具 | 0 |
| **工程** | `codebase` / `wiki` | 代码仓库 / 知识文档 | 10 |
| **事件** | `incident` / `change` | 故障 / 变更 | 0 |

#### `domain` 的位置：与 portfolio 平行的第二条召回路径

**不做 `portfolio — domain` 层级。** domain 与 portfolio **平行地直连 app**，
边规则与 `tool` / `codebase` 同构（"跟 app 关联即可"）：

```
portfolio ──portfolio_link──┐
                            ├──→ app
domain    ──domain_link─────┘
```

**为什么平行比层级好**：

- **不破坏已定的节点规则**（app 仍不直连 app，业务分类仍不经过 app）
- **多一条独立召回路径** → §3.4 的交叉验证多一份素材：同一 app 被 portfolio 与 domain
  分别命中，证据强于单条路径
- **粒度可不同**：portfolio 粗（业务领域）、domain 细（细分职能），
  对"打印工单没反应"这种细粒度描述，domain 可能命中而 portfolio 不命中

**代价**：domain 不归属任何 portfolio，"它比 portfolio 更细"只是个**约定**，图上表达不出来。
如果将来需要"这个 domain 属于哪个 portfolio"，那要补 `portfolio — domain` 边——
**届时它仍然不与本文的分层约束冲突**（portfolio 与 domain 都不是 app）。

### 2.2 边类型（11 → 13）与**分层约束**

关键设计：**给每类边打 `layer` 标签**，把"app 之间不能直接关联"这条规则变成**可校验的约束**，
而不是靠人记住。

| layer | 边类型 | 端点 | 方向 | 来源 |
|---|---|---|---|---|
| **business** | `enterprise_journey` | enterprise — journey | 无向 | 新模型 |
| | `journey_link` | journey — portfolio | 无向 | 已有 |
| | `portfolio_link` | portfolio — app | 无向 | 已有 |
| | `domain_link` | **domain — app** | 无向 | **本轮新增** |
| **runtime** | `calls` | app → app | 有向 | **本地新增**（见下） |
| **support** | `app_codebase` | app — codebase | 无向 | **本轮提升**（原 `refs.repo_ref`） |
| | `support` | app — team / app — wiki | 无向 | 已有（`support_kind` 区分） |
| | `agent_watches` | agent → app | 有向 | 已有 |
| | `tool_integrates` | tool → app | 有向 | 已有 |
| **event** | `incident_cluster_app` | incident → app | 有向 | 已有 |
| | `change_cluster_app` | change → app | 有向 | 已有 |
| | `change_causes_incident` | change → incident | 有向 | 已有 |
| | `storm_fan_out` | incident → incident | 有向 | 已有 |

**删除**：`cross_journey_link`（随 `cross_journey_hub` 一并删除）。

#### 分层约束（enforced，不是文档约定）

```
business 层：app 之间禁止直接边
```

加进 loader 校验清单：`layer == "business"` 的边，其两端**不得同时是 app**。
这样：

- 你的规则「app 与 app 之间不能直接关联，要通过 portfolio」**由校验器保证**——
  将来界面里想连一条 app—app 的业务归属边，会被直接拒掉
- `calls` 归入 **runtime 层**，语义是**观测到的运行时依赖事实**，不是业务归属。
  它是 `get_service_topology`（爆炸半径 / 上游根因）的唯一数据源，必须保留

> **为什么不是简单删掉 `calls`**：删掉它，`get_service_topology` 立即失去数据源，
> 而它是线上已交付、有 12 个测试钉住的生产形态工具。"爆炸半径"是诊断的核心概念，
> 不能因为分类树要保持干净而拿掉。**分层**这个建模既守住了你的规则，又保住了能力。

### 2.3 App 兼作云环境

App 节点加 `attributes.kind ∈ {application, environment}`：

```json
{ "id": "app:order-service", "type": "app", "name": "order-service",
  "attributes": { "kind": "application", "namespace": "order", ... } }

{ "id": "app:azure-cn-north3", "type": "app", "name": "azure-cn-north3",
  "display_name": "azure-中国-北三区",
  "attributes": { "kind": "environment" } }
```

**为什么用属性而不是新节点类型**：app 与云环境的关系是"所在"，但按分层约束 app 之间
不能直连（business 层禁止），而"部署在"既不是业务归属也不是运行时依赖——用 `kind`
区分开后，LLM 判断相关性时能直接排除环境节点（工单问的是服务，不是机房）。

**留一个开放问题**（§8）：`app` 与它所在 `environment` 的关系要不要显式建模？如果要，
它是一类新边（`deployed_in`，app → app），会与"app 不直连"的规则冲突——需要先定这个。

### 2.4 业务语义：让业务域"可检索 + 可判断"

**这是整个设计的地基。** 一个裸的 `portfolio:order` 对 LLM 毫无用处。

> **Journey / Portfolio 不是独立主体，是 service 的「业务标签层」。** CMDB **以 service
> 为主体**构造，业务层挂上去的目的是**给 service 提供可被问题命中的语义锚点**。
>
> 由此推出一条容易写反的规则：**`keywords` 该写什么，不由"这个业务域是什么"决定，
> 而由"用户会怎么描述它出问题"决定。** 写「售后服务选择」是业务视角；
> 写「退货 / 换货 / 保修查询 / 售后入口进不去」才是问题视角——后者才是能被 ticket
> 命中的。详见 §3.4。

业务域节点必须带两类**用途不同**的字段：

| 字段 | 用途 | 谁消费 | 为什么不能合并 |
|---|---|---|---|
| `keywords` | **检索键**——用户/工单里实际会说的词 | 确定性召回（不用模型，快、可扩展到 282 apps） | 检索要的是**高召回**：宁可多捞，词可以很粗 |
| `description` | **判断依据**——这业务域干什么、边界在哪 | LLM 推理 | 判断要的是**高精度**：需要语义，不是关键词 |
| `capability` | 业务能力名（参考站 Journey "Engage" 就是它） | 展示与归类 | 给人和 UI 看 |

各类节点的属性模型：

```json
// enterprise —— 企业功能
{ "attributes": { "description": "面向个人消费者的售后维修与保养业务",
                  "keywords": ["售后", "维修", "保养", "理赔", "退换"] } }

// journey —— 用户旅程
{ "attributes": { "capability": "After-sales Service",
                  "description": "用户从报修到服务完成的端到端旅程",
                  "keywords": ["报修", "预约保养", "上门服务", "服务进度"] } }

// portfolio —— 业务领域（粗粒度）
{ "attributes": { "description": "维护车辆/设备的保养计划与执行记录",
                  "keywords": ["保养计划", "保养记录", "保养提醒"] } }

// domain —— 业务细域（细粒度，与 portfolio 平行直连 app）
//   ★ 这里的 keywords 最容易写对：它就是"用户在描述这个细分职能时会用到的词"
{ "attributes": { "description": "打印保养工单并跟踪打印结果",
                  "keywords": ["打印工单", "工单打印不出来", "打印没反应", "打不出单"] } }

// app —— 业务角色（新增可选字段）
{ "attributes": { ..., "business_role": "负责扣款与退款" } }
```

**按本轮决定，内容留空。** 但要说清后果：

> ⚠️ **今天这一层没有内容，`scope` 节点只能退到 App 级真实字段**
> （`name` / `namespace` / `owner` / `tech`）做匹配——也就是上一轮建的那套。
> **业务域内容录入之前，这一环的上限就在那。** 这也正是为什么现在要把 schema 定死：
> 构建界面一上线，录入的就是这些字段。

顺带，这也**修掉了上一轮那个"中文匹配不上"的问题**——不是靠改匹配算法，是靠 `keywords`
里**录入用户真正会说的词**。「支付超时」匹配不上 owner「支付团队」，但如果
`portfolio:payment` 的 `keywords` 里有"支付"、"付款"、"扣款"，召回就成立了。

### 2.5 ⚠️ 现有 6 个 Portfolio 必须清理

当前这 6 个是从 `namespace` 派生的：`order` / `payment` / `inventory` / `logistics` /
`common` / `account`。

**它们在新模型下是错的**：`namespace` 是 **k8s 部署分组**，不是**业务领域**。新模型里
Portfolio 应该是「销售库存」「售后服务选择」「配件购买」这类业务概念。`common` 尤其荒谬
——它装着两个 owner 完全不同的服务（`notification-service`「平台基础」+
`audit-service`「安全合规」）。

**处置**：在业务域数据录入时删除这批派生节点。**不要**把它们改名沿用——那会把
"部署分组"的语义残留带进业务分类。

---

### 2.6 载体：**保持 JSON 文件**（DB 方案已评估并推迟）

**2026-09-15 复核后决定：CMDB 载体继续用 JSON 实体文件**（`af3e88a` 的方案不变）。

一度考虑过改为 PostgreSQL（复用 agentflow 的库、按租户建表），复核后**推迟**。

#### 为什么查询不需要数据库

逐条对照我们要的查询能力——**JSON（加载进内存）在每一项上都不差**：

| 查询需求 | JSON（内存） | PostgreSQL |
|---|---|---|
| 精确：结构化字段 | `n["attributes"]["tech"] == "Java"` | `attributes->>'tech' = '...'` |
| 精确：`keywords` 成员 | `set(n["keywords"]) & want` | `keywords && ARRAY[...]` |
| 子串包含 | `term in text` | `ILIKE '%…%'` |
| 模糊（相似度） | `difflib.SequenceMatcher`（stdlib） | `pg_trgm` |
| 多层下钻 | 递归遍历（**已实现**） | 递归 CTE |
| 规模 | 26 → 几万节点都在内存 | 无上限 |

**决定性的是数据量**：26 个节点、到 282 apps 也只有几百 KB。整份读进内存做匹配，
比走 SQL **更快**（无网络往返、无查询计划），且不需要 DB 驱动、不需要 DSN、
**不改变 MCP server "无状态只读"的性质**。

#### 数据库的价值在写入侧，不在查询侧

DB 真正解决的是：构建界面**增删改**节点/边、并发编辑、**单条更新**（不必全量重写文件）、
事务。这些 JSON 确实做不了。**但当界面还不存在、录入只有一两个人时，这是为一个尚未到来的
需求付运维成本。**

#### ⚠️ 记一笔：本方案评估中我自己论证过头的两条

留在这里避免以后重犯：

| 我当时的说法 | 实际 |
|---|---|
| "三种查询用 SQL 表达远比 Python 手写索引干净" | ❌ **说过头了**。内存匹配的 Python 表达同样干净，而且已经写完并有 86 个测试 |
| "引用完整性/唯一性由 DB 约束保证" | ⚠️ 成立，但**跨字段约束 SQL 也表达不了**（`id` 前缀等于 `type`、business 层禁 app—app），这部分优势比自己说的小 |
| "界面编辑数据库正常，编辑 JSON 别扭" | ⚠️ 成立但不致命。界面读写一个 JSON + 文件锁，对单用户/少用户足够 |

#### 推迟不是放弃——触发条件

出现以下任一条时重新评估：

- 构建界面要做，且**需要多人并发录入**
- 目录规模涨到**装不进一次 LLM 上下文**（届时 §3.4 的兜底失效，见 §8.2）
- 需要**审计每一次变更**（谁在什么时候改了哪条边）

#### 附：DB 方案的表结构草案（保留备用，将来可直接用）

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- 模糊匹配；不需要 pgvector（已放弃语义匹配）

CREATE TABLE cmdb_ontology (              -- 本体声明，替代 JSON 的 ontology 块
    tenant_id text NOT NULL, kind text NOT NULL, key text NOT NULL,
    label text NOT NULL, label_zh text, spec jsonb NOT NULL,
    PRIMARY KEY (tenant_id, kind, key)
);

CREATE TABLE cmdb_nodes (
    tenant_id    text NOT NULL,
    id           text NOT NULL,                    -- 'app:order-service'
    type         text NOT NULL,
    name         text NOT NULL,
    display_name text,
    attributes   jsonb  NOT NULL DEFAULT '{}',
    tags         text[] NOT NULL DEFAULT '{}',     -- 静态横切标签（派生指标不得入内）
    keywords     text[] NOT NULL DEFAULT '{}',     -- ★ 关键词匹配的主力
    refs         jsonb  NOT NULL DEFAULT '{}',
    notes        text,
    search_text  text GENERATED ALWAYS AS (
        name || ' ' || coalesce(display_name,'') || ' ' || array_to_string(keywords,' ')
    ) STORED,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, type, name)
);

CREATE TABLE cmdb_edges (
    tenant_id text NOT NULL, id text NOT NULL, type text NOT NULL,
    from_id text NOT NULL, to_id text NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, type, from_id, to_id),
    FOREIGN KEY (tenant_id, from_id) REFERENCES cmdb_nodes (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, to_id)   REFERENCES cmdb_nodes (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX cmdb_nodes_search_trgm ON cmdb_nodes USING gin (search_text gin_trgm_ops);
CREATE INDEX cmdb_nodes_keywords     ON cmdb_nodes USING gin (keywords);
CREATE INDEX cmdb_edges_from        ON cmdb_edges (tenant_id, from_id);
CREATE INDEX cmdb_edges_to          ON cmdb_edges (tenant_id, to_id);
```

⚠️ 届时仍需注意：**DDL 归属**（表结构属 CMDB 领域但库由 agentflow 的 `tenantctl` 建，
若不纳入其 `schema_versions` 管理，两边版本会静默分家）。

---

## 3. 诊断链新增 `scope` 节点

### 3.1 位置与数据流

```
triage ──┬──→ scope ──┬──→ logs
         │            ├──→ trace
         │            ├──→ metrics
         └────────────┴──→ infra        ← 四条边都要显式声明，见 §3.3
```

`scope` 在 triage 之后、四个取数节点之前。**服务名必须先定下来，四个取数节点才有意义**——
现在它们拿到的只有 triage 的一句摘要。

### 3.2 节点定义

```yaml
scope:
  agent: service-scoper
  join: all
  required_edges: [triage]
  params:
    bug: "$.nodes.triage.output.summary"
    bug_report: "$.inputs.bug_report"     # ← 必须单独传，见下
    start_time: "$.inputs.window_start"
    end_time: "$.inputs.window_end"
  on_failure: abort                        # 定不出服务，后面全是空转
```

**`bug_report` 必须单独传**：triage 的输出 schema 只有
`{symptom_type, severity, summary}`，`cmdb_ci.name` 在 triage 之后就丢了。这是当前链路的
一个真实缺陷，不补上这条，工单自带的 service 信息永远到不了 `scope`。

### 3.3 ⚠️ 四个取数节点必须改 `join`——否则会静默失效

`params` 的引用本身没问题：`check_params_refs`（`core/dag.py:255-264`）允许引用
**任意传递上游**，不限于直接前驱。所以 `logs` 可以同时引用 `triage` 与 `scope`。

**但默认 `join: any` 的语义是"至少一条 ACTIVE 入边即 READY"**
（`executor/dag_executor.py:259-260`）。`logs` 有了 triage 和 scope 两条入边之后，
**triage 一完成它就被调度**——此时 `scope` 还没跑，`candidate_services` 是 `None`。

所以四个取数节点每一个都要写：

```yaml
logs:
  join: all
  required_edges: [triage, scope]
```

这正是 `bug-fix-scenario2.yaml` 里 `locate` 节点**已经踩过**的坑，YAML 注释原话：

> 默认 join: any 会让它在 triage 一完成就被调度（trace 尚未跑完 → failing_service 为 null，
> 导致 code-locator 只能按 ticket 的 subcategory 猜服务，可能定位错仓库）

**代价**：四个取数节点不能再在 triage 完成的瞬间启动，必须等 scope。这是**必要**的
——它们本来就依赖服务名，没有服务名的"并行取数"是假并行。

### 3.4 节点内部：问题域分层 ↔ 方案域分层

#### 核心思路：映射不是一步到位的

**问题描述常常根本不含服务名。** "打印工单没反应"里没有任何技术实体——
直接匹配 service 必然落空。所以映射必须**双向、分层**：

| | 做什么 | 目的 |
|---|---|---|
| **问题域侧** | 关键词提取 + **高层抽象**——把具体现象抬到业务概念 | 让问题能在**不同层次**找到落点 |
| **方案域侧** | CMDB **以 service 为主体**构造，但把 service 与 domain / portfolio / journey 的关系建好 | 让任一层落点都能**下钻**到 service |

两侧对接的地方就是匹配：**问题在某一层命中 → 沿 CMDB 关系下钻到 service**。

> **这改变了 §2.4 的定位**：Journey / Portfolio **不是独立主体，是 service 的「业务标签层」**。
> 它们的 `keywords` / `description` 存在的意义，是给 service 提供**可被问题命中的语义锚点**。
> 所以 `keywords` 该写什么，不由"这个业务域是什么"决定，而由"**用户会怎么描述它出问题**"决定。

#### ticket 类型是异质的，映射目标也不同

| ticket 类型 | 例子 | 真正要找的 | 主用策略 |
|---|---|---|---|
| **技术故障** | "order-service 报错" | 显式服务名 | **精确名匹配**（app 层） |
| **业务操作问题** | "打印工单没反应" | 承担「打印工单」职能的服务 | **高层抽象 → journey/portfolio 层命中 → 下钻**（依赖 `keywords` 覆盖） |
| **变更 / 升级** | "升级 Java 版本" | 哪些服务用了 Java | **属性过滤**（`attributes.tech`）——确定性、精确 |

对变更类 ticket 问"定位**异常**服务是**问错了问题**——"升级 Java"没有异常，
要找的是"哪些服务在变更影响范围内"。

#### 流程

```
问题描述（自然语言 + bug_report）
   │
   ▼ ① 关键词提取 + 高层抽象（agentflow，LLM）
   │     具体现象 → 高层业务概念
   │     "打印工单没反应" → 业务动作「打印工单」、业务域「工单管理」
   │     "升级 Java"      → 技术栈「Java」（不是业务域）
   ▼
   ② 分层关键词匹配（MCP server，**无模型**，逐层尝试）
   │     ├─ app 层        ← name / namespace / owner / tech
   │     ├─ domain 层     ← keywords（**粒度最细，误召最少**）
   │     ├─ portfolio 层  ← keywords（精确包含 + 模糊）
   │     ├─ journey 层    ← keywords / capability
   │     └─ enterprise 层 ← keywords
   ▼
   ③ 沿图下钻（MCP server，图操作）
   │     命中的每个节点 → 沿 journey_link / portfolio_link / domain_link
   │                       取其下全部 app
   ▼
   ④ 合并 + 交叉验证 + LLM 判断（agentflow）
   │     多层命中同一 app → 置信度叠加（见下）
   ▼
   候选服务集
```

#### 匹配只用关键词——精确 + 模糊，不做语义

**2026-09-15 决定：放弃语义（embedding）匹配，只做关键词的精确与模糊匹配。**

| 匹配类型 | 机制 | 例 |
|---|---|---|
| **精确** | 结构化字段等值 / `keywords` 数组成员 / 子串包含 | `tech = 'Java / Spring Boot'` |
| **模糊** | `pg_trgm` 相似度 / 编辑距离容忍拼写差异 | "order-sevice" → "order-service" |

这条决定带来两个后果，**一好一坏**：

- ✅ **架构变干净**：MCP server 不再需要任何模型依赖，回到纯粹的只读数据服务；
  也**与业界实践一致**（调研显示业界从不拿 embedding 做服务召回，见 §8.2）
- ⚠️ **`keywords` 词表从"重要"变成"唯一"**：语义匹配原本能兜住词表未覆盖的说法
  （"打印工单没反应"里没有「打印」也能靠语义捞到）；现在没有第二道防线——
  **词表里没有，就是彻底漏召**。所以 §2.4 那条"`keywords` 由用户会怎么说决定"的规则
  从建议升级为**硬要求**，`cmdb-business-terms-draft.md` 里那列占位词必须用真实工单词替换

#### 兜底：召回为空 → 回退全量给 LLM

**关键词匹配的漏召率高于语义匹配，所以必须有兜底**：当所有层的匹配**都为空**时，
**不要返回空结果就结束**——把**全量目录**交给 LLM 判断。

这在我们的规模下可行（26 节点；到 282 apps 仍在单次上下文内），且是纯关键词方案唯一
可靠的防线。**目录大到装不下时，这个兜底会失效——那时才需要重新评估召回策略**
（届时的选项见 §8.2 的调研结论，而不是反过来先上向量）。

#### 层级衰减：命中越高层，越不能单独作数

**这是分层映射的固有风险**——命中 enterprise 层（如"零售业务有问题"）下钻出来可能是
**全量服务**，等于没缩。所以必须衰减：

| 命中层 | 下钻覆盖 | 初始置信 | 说明 |
|---|---|---|---|
| `app` | 1 个 | **high** | 确定性最高 |
| `domain` | 该细域下几个 | **high**（略低于 app） | 粒度最细，误召最少 |
| `portfolio` | 该域下若干 | medium | |
| `journey` | 跨多域 | low | |
| `enterprise` | 可能全量 | **极低** | **不足以单独作为候选依据**——必须与更细层的命中共同出现 |

#### 交叉验证：分层映射最大的增益

**同一 app 被多个层次独立命中 → 置信度显著提高。** 这是可计算的信号，不是感觉。
`domain` 与 `portfolio` **平行直连 app**（§2.1），所以它们天然构成两条**互相独立**的路径：

```
"打印工单没反应"
   ├─ domain「工单打印」  命中 → 下钻 → {ticket-service}
   ├─ portfolio「订单交易」命中 → 下钻 → {order-service, pricing-service}
   └─ journey「售后工单」  命中 → 下钻 → {ticket-service, notification-service}
                                          ↑
                        ticket-service 被 2 条独立路径命中 → 置信度叠加
```

**注意 `domain` 在这里的价值**：如果 `keywords` 只写到 portfolio 那层（「订单交易」），
"打印"这个词根本不在它下面——**domain 是让细粒度描述也能命中的那一层**。
这也正是把 domain 设成"与 portfolio 平行"而非"其子级"的实际收益：
两条路径各写各的 `keywords`，互不挤占。

命中路径数 / 命中层数进 `reasons`，也是 LLM 判断的明确先验。**这是"从不同层次映射到
解决方案域"这句话最实在的收益**——单层匹配只有"命中/未命中"，分层匹配有"被几条独立
证据支持"。

#### 边界：② ③ 住 MCP server，① ④ 住 agentflow

MCP server 是只读数据服务，**不要为了"更聪明"给它加模型依赖**（它现在全是
`readOnlyHint=True` 的工具，agentflow 侧据此自动放行）。抽象与判断需要模型，归 agentflow；
匹配与图操作是确定性的，归 server。

#### 精确优先，模糊只在精确未命中时补位

匹配策略有明确的**优先级顺序**，不是同级并列：

1. **结构化精确**（`name` / `namespace` / `tech` / `keywords` 成员）——确定性最高
2. **子串包含**——确定性，但比等值宽松
3. **模糊**（`pg_trgm` 相似度）——**只在上面都没命中时才启用**

理由：模糊匹配是**误召和漏召的共同来源**。用它兜底可以，用它主查会把噪声灌进候选集。

#### 为什么召回层必须存在（不能全给 LLM）

10 个服务时确实可以全塞给 LLM。但目录一大就爆上下文。召回负责**把大目录收到 ~10**，
LLM 只在这个短名单上做判断。**这是设计能扩展的前提**，也是 §2.4 里
`keywords` 必须存在的原因。

注意这不是绝对约束——**兜底策略（召回为空 → 全量给 LLM）本身就是"全给 LLM"**，
只是限定在"关键词一条都没命中"时才触发。

### 3.5 `service-scoper` 工具与输出

工具：
- `query_entity_graph(node_types=["app", "portfolio", "journey"])` —— 拉业务域全景作判断依据
- `get_service_topology(service, hops=2)` —— 确定性扩展
  （`upstream`=爆炸半径 / `downstream`=上游根因）

输出 schema：

```json
{
  "intent": "fault | change | inquiry",
  "abstractions": ["打印工单", "工单管理"],
  "candidate_services": [
    { "service": "ticket-service",
      "confidence": "high",
      "impact": "medium",
      "matched_layers": ["journey", "portfolio"],
      "hit_paths": 2,
      "reasons": ["journey「售后工单」命中 → 下钻",
                  "portfolio「工单打印」命中 → 下钻",
                  "2 条独立路径交叉命中"]
    }
  ],
  "primary_service": "ticket-service",
  "expand_search": false,
  "summary": "..."
}
```

**`confidence` 与 `impact` 保持两个独立的轴**（沿用上一轮的设计）：前者是"它有关的证据
有多强"，后者是"如果有关影响多大"。把 `tier1` 折进置信度会让一个仅凭拓扑相邻的候选
拿到与工单指定服务同等的标签——那是把重要性冒充成可能性。

**`matched_layers` / `hit_paths` 是分层映射的产物**（§3.4）：命中层越细、独立路径越多，
证据越强。这两个字段不是装饰——它们是 `confidence` 的**可核对依据**，
也是 LLM 判断时的明确先验。

⚠️ **注意 `abstractions` 可能与 `candidate_services` 断层**：如果高层抽象做出来了
（`["打印工单"]`）但在 CMDB 里**找不到任何落点**，说明**业务域语义没录入**——
这要**如实上报**（`candidate_services: []` + 说明），而不是硬凑一个服务出来。
这正是 §2.4 说"业务语义是地基"的实际含义。

---

### 3.6 输入不足：停止并请求补充（不得硬推）

**原则**：ticket 信息不足以支撑后续推导时，**停止流程让用户补充**——而不是拿现有信息
硬凑一个候选集。这与本设计的 fail-closed 精神一致（§3.5 的"断层要如实上报"是同一个原则的
局部版本，这一节是它的流程级版本）。

#### 什么算"不够"

| 情形 | 判定 |
|---|---|
| 高层抽象做出来了，但**在任何层都找不到落点** | 不够——业务域语义没录入，或描述太模糊 |
| 命中的全部候选 **置信度都是 `low`** 且 `hit_paths` 均为 1 | 不够——等于没有可区分的证据 |
| ticket 缺**时间窗**（`window_start`/`window_end`） | 不够——数据查询工具是必填的（v5.5 §7.1） |
| 意图无法判定（既不像故障也不像变更） | 不够 |

**注意最后一条与 `on_failure: abort` 的区别**：abort 是**失败**（节点跑挂了），
而"信息不足"是**正常但无法继续**——两者的终态、可恢复性、对用户的措辞都不同，不能混用。

#### 机制：原地暂停 vs 停止后重跑

| | (A) 停止本次 run + 重新发起 | (B) 新增 `kind: clarification` 节点，原地暂停 |
|---|---|---|
| 复用 | 全部现有机制 | 需新 node kind + 状态 + CAS + 端点 |
| 用户体验 | 补充后**重跑**，前面的诊断白做 | 补充后**从断点继续**（checkpoint 已落盘） |
| 与"停止流程"字面 | 部分符合 | **符合** |
| 成本 | 低 | 中（但是 `kind: approval` 的自然扩展） |

**推荐 (B)**，理由是平台已有全部底层能力：审批节点已经实现了「暂停 → 落盘 → 人给输入 →
`output` 落地 → resume」，clarification 只是把**二值决定**换成**结构化自由输入**。

**关键洞察（解掉了一个看似冲突的地方）**：run 的 `inputs` 在创建时冻结（§8.7 版本冻结），
所以补充的信息**不能**走 inputs。但它**可以走节点输出**——审批节点就是这么做的
（人的决定成为该节点的 output，下游用 `$.nodes.<id>.output` 引用）。
**输入冻结因此不构成障碍**，不需要解冻 inputs，也就不会破坏版本冻结语义。

#### 需要新增的东西（若走 B）

1. `kind: clarification` 节点类型
2. 一个与 `WAITING_APPROVAL` 并列的状态（如 `WAITING_INPUT`），同样进
   `ACTIVE_RUN_STATUSES`（否则并发配额会提前释放）
3. 节点输出 schema：`{ question, needed_fields: [{field, why, example}] }`
4. 一个提交端点的语义（参照 approve/reject），**必须带 CAS + 超时**——
   超时语义与审批不同：审批超时走拒绝路径，**clarification 超时应走"信息不足终止"**
   而不是默认一个答案（默认答案就是硬推）
5. ticket 状态加 `waiting_input`（现在只有 `new / running / resolved / failed`）

⚠️ 待定：**超时后要不要自动终止**。自动终止更安全；但如果用户只是慢，
误杀会让人重来。建议：超时后**不自动终止**，转为"长期挂起"并通知，由人或 sweeper
按租户策略处置——**但这条需要你定**。

---

## 4. 置信度驱动取数广度

| 置信度 | 取数行为 |
|---|---|
| `high` | 只查 `primary_service` |
| `medium` | 查 top-3 候选 |
| `low` | `expand_search: true` → 查 top-5 + 各自一跳拓扑邻居 |

低置信时 token 开销上升是**有意**的——故障说不清时本来就该多查。

---

## 5. 工单 service 的证据权重

按你的要求，落成三条规则：

1. **进候选时起始置信度直接给 `high`**，理由记为「工单 cmdb_ci 指定」——比 LLM 从文本
   推出来的权重大
2. **LLM 可以推翻它**，但**推翻必须在 `reasons` 里给出反证**（如"该服务窗口内无任何
   ERROR 日志，而 keywords 命中的是另一个服务"）
3. **即使工单指定了服务，也仍然扩展一跳**

第 3 条是关键：**"不能完全依赖"不只是"要怀疑它错"，更是"它对，但不够"**——工单写的
服务是**症状出现的服务**，不等于**根因所在服务**。这正是 `get_service_topology` 里
`downstream`=上游根因那套语义的用武之地。

---

## 6. `trace.failing_service` 降格为证据

现状：它是**唯一**真正驱动下游的信号。新位置：

| | 信号 | 时机 | 性质 |
|---|---|---|---|
| **主** | `scope` 基于 CMDB 的定位 | 早 | 宽、有业务语义 |
| **证据** | `trace` 的 `failing_service` | 晚 | 窄，但是**真实运行时证据** |

**两者不一致时不要丢掉一方，而是记下来**——这往往正说明"症状服务 ≠ 根因服务"，
是有价值的信号。

具体接法：`locate` 节点**保持不变**（继续用 `failing_service`），把它与
`scope.primary_service` 一起交给 `root-cause` 节点做交叉判断。**只改 `rca` 的 params
和提示词，不动 `locate` 的接线**——把改动面控制在最小。

---

## 7. 实施清单

### 7.1 `aiops-mcp-servers`（CMDB / 数据面）—— ✅ **本体部分已实施（2026-09-15）**

| 项 | 改动 | 状态 |
|---|---|---|
| 节点类型 | 删 `cross_journey_hub`、**新增 `domain`**（净 12 类）；`app.attributes` 加 `kind`（必填）、`business_role`（可选）；业务层四类各加属性模型（`description` / `keywords` / `capability`） | ✅ |
| 边类型 | 删 `cross_journey_link`；新增 `enterprise_journey`、`domain_link`、`app_codebase`；给全部边加 `layer`（**11 → 13**） | ✅ |
| 校验 | **business 层禁止 app—app**（在 **ontology 声明层**强制，见 §3）；新增业务层属性模型 | ✅ |
| 实体文件 | `refs.repo_ref` → 10 条 `app_codebase` 边；**删除 6 个派生 Portfolio**；业务语义字段留空 | ✅ |
| schema | 重新导出 `docs/cmdb-entities.schema.json`；`docs/cmdb-entities.md` 补分层 / domain / 业务语义章节 | ✅ |
| 工具 | 新增确定性召回能力；`infer_candidate_services` 降为**证据提供者** | 🟡 **部分完成** |

**验收结果**：`test_cmdb_backend.py` **零改动通过**；`get_service_topology` / `locate_repo`
改动前后输出**逐字节相同**（默认与 `DATASOURCE_REPO_ROOT` 两种模式均验证）——
本体大改（删 hub、加 domain、清空业务层、`refs`→边、加 `kind`）**完全没有影响对外契约**。
测试 144 → 164；全仓 270 通过；ruff / mypy 干净。

#### 工具部分：§3.4 的分层匹配已实现

**已做**：扫**全部节点类型**（不只 app）、业务层命中后沿业务边**下钻**到 app、
多路径命中**升一档**（交叉验证）、置信度按**证据类型**分档
（`high` 只留给直接证据：症状服务 / 工单 ``cmdb_ci`` 指定）。

**仍未做**：① 意图分类（§3.4 ①，决定"找异常服务"还是"找变更影响范围"）；
② 兜底「召回为空 → 全量给 LLM」（属 agentflow 侧行为，不在本工具内）。

#### ⚠️ 过程中修掉的 5 个匹配缺陷——全是"看起来在工作、实际没工作"

| # | 缺陷 | 症状 |
|---|---|---|
| 1 | `attrs.get("name")` 永远为空 | **按服务名匹配从未生效过**——服务名在 `node["name"]`，不在 attributes 里 |
| 2 | 匹配器不读 `keywords` | v5.7 新加的字段是死数据，加了等于没加 |
| 3 | 只扫 app 节点 | 业务层节点（journey/portfolio/domain）不可能被命中，加了只是装饰 |
| 4 | `_MIN_TEXT_MATCH_LEN = 3` | 为挡 `tech="Go"` 而设，却把 **33 个双字中文关键词全杀**——中文是双字词密集的语言，3 字符门槛等于关掉中文匹配 |
| 5 | 字段值**整串**匹配 | `tech: "Java / Spring Boot"` 整串永远匹配不上「升级 Java 版本」，必须按分隔符切**词元** |

> ⚠️ 其中 #1 被一个**假命题测试**掩盖了很久：`assert any("name" in reason)` 之所以通过，
> 是因为 reason 里的 `namespace` 恰好包含子串 `name`。已改为断言精确的 `name=order-service`。
>
> 这 5 个缺陷解释了为什么「加字段」在修复前**一点效果都没有**——字段加了，
> 但读它的代码要么没写、要么写错了键、要么被别的守卫挡掉。

### 7.2 `multi-agent-workflow`（编排层）—— ✅ **已实施并 E2E 验证（2026-09-16）**

| 项 | 改动 | 状态 |
|---|---|---|
| 新 agent | `service-scoper`（提示词 + `CandidateServicesSchema`），注册进 `DIAGNOSE_AGENTS` / `AGENT_STAGES`(detect) / `AGENT_DESCRIPTIONS` | ✅ |
| workflow | 新增 `scope` 节点；`logs`/`trace`/`metrics`/`infra` 加 `join: all` + `required_edges: [triage, scope]`；**两个 pipeline 都改了** | ✅ |
| `rca` | params 加 `scope_primary`；提示词加「`scope` 与 `trace` 不一致时如何裁决」 | ✅ |
| prompt | 四个取数 agent 加 `_SERVICES_RULE`（按置信度档位取用候选；为空才退回宽查询） | ✅ |

#### 实现时踩到、值得记住的三点

1. **`required_edges` 必须是「直接」上游，传递上游不算**。`_check_join_consistency`
   （`core/dag.py:212`）拿 `node.upstreams`（= in_edges 的 source）做子集判断。
   `logs` 声明 `required_edges: [triage, scope]` 就必须**同时有** `triage → logs`
   与 `scope → logs` **两条直接边**——只写后者会报
   `required_edges 不是其上游: ['triage']`。（这正是 `join: all` 那个坑的另一半：
   前者是"不声明会过早调度"，后者是"声明了但没有对应边会加载失败"。）
2. **新 agent 默认没有任何 MCP 工具**。`agent_configs.mcp_server_ids` 是两态语义
   （NULL/`[]` = 无 server）。加完 agent 必须显式绑定：
   `PUT /agent-configs/service-scoper -d '{"mcp_server_ids":["<mid>"]}'`
   ——对内置 agent 是 upsert，不传的文本字段回退内置默认，不会覆盖提示词。
3. **run 用的是库里保存的 workflow，不是仓库里的 YAML**。改完 YAML 必须写回：
   `PUT /workflows/{wid} -d '{"name":..., "yaml":...}'`，否则 run 跑的还是旧流程
   （本次就差点漏掉——库里那份是改之前保存的）。
   **这是"新租户初始化缺播种"的一个侧面**：`tenantctl provision` 只建库建表、
   不播种 workflow 与数据面绑定，新租户的 `workflows` 表是空的 → 起不来诊断。
   **已登记为 `docs/TODO.md` §13**（含要做的事：provision 时从仓库 YAML 幂等播种、
   数据面绑定配置化、补同步命令）。

#### E2E 实测（工单「订单服务打印结账单无反应」，租户 `otr`）

- `scope` 节点 `done`，12.7s，**调用了 `infer_candidate_services`**，输出
  `intent=fault`、`primary_service=order-service`、`expand_search=true`，
  候选含 order-service(high) + 9 个拓扑邻居(medium)，并给出 5 条 `abstractions`
- **join 语义实测有效**：`scope` 于 `02:41:58` 结束，四个取数节点**统一在
  `02:42:00` 启动**——若 `join` 未生效它们会在 triage 结束（`02:41:45`）就启动，早 15 秒
- `rca` 明确做了交叉核对：*"`scope_primary=order-service` 与工单语义一致"*
- 全链跑通至审批节点（13 done / 1 `waiting_approval`），817k tokens / $0.07
- **诚实度符合预期**：`scope` 主动标注"1 跳映射为**静态拓扑而非运行时观测**"；
  `rca` 在证据缺失时写"窗口内全服务日志 0 条…**不可当作 0**"而非编造

### 7.3 验收判据

- CMDB：`business` 层 app—app 连边被拒（反例测试）；`calls` 归 runtime 层后
  `get_service_topology` 的 12 个测试**零改动通过**
- workflow：`logs` 在 `scope` 未完成时**不得**被调度（回归测试，对应 scenario2 里
  `locate` 那个坑）；`cmdb_ci.name` 能被 `scope` 读到
- 端到端：一张带 `cmdb_ci.name` 的工单 → `scope` 输出的候选集里该服务置信 `high`，
  且**包含它的一跳邻居**

---

## 8. 业界调研依据（2026-09-15）

对商业 AIOps（Datadog / Dynatrace / ServiceNow）、CMDB/ITSM 厂商、开源生态（Prometheus +
Alertmanager / Zabbix）做了对抗性验证调研（27 来源 / 125 主张 / 25 条验证 / 19 存活）。
以下是**影响本设计**的结论。

> ⚠️ **样本局限（读这一节前必须知道）**：存活结论只来自上述五家。点名的新 Relic、
> Splunk ITSI、Moogsoft、BigPanda、PagerDuty、BMC Helix、OpenText、OpenTelemetry、
> SkyWalking、Grafana **零存活结论**。证据几乎全部来自**厂商自己的文档**，不是独立测评。
> 多条"没有 X"的结论是**证据缺失**（argument from absence），不是正面否证。

### 8.1 业界主路径是遥测自动发现，CMDB 是辅助

| 产品 | 服务图从哪来 |
|---|---|
| Datadog Service Map | **APM 插桩的服务**；边 = 观测到的聚合调用；无人工注册，30 天无 trace 老化 |
| Dynatrace Smartscape | 从摄入的指标/事件/日志/链路**自动创建更新**，"你什么都不用手工配" |
| ServiceNow | 拓扑来自 **Basic Discovery**（自动发现）或 Service Maps |

CMDB 与它们的连接方向是**出向**的（Dynatrace 把 Smartscape **导出进** ServiceNow CMDB）。
ServiceNow 自己把「基于 tag 的告警聚类」定位成"**在没有一个填充完整的 CMDB 时**也能开始
用 AIOps"——厂商自己用负面框架描述 CMDB。

> ⚠️ 但"CMDB 不完整会限制告警关联"这个具体主张**在验证中被驳回（0-3）**。
> 所以"CMDB 不可靠"这个印象**主要建立在厂商定位与证据缺失上，没有实测研究支撑**。

**对本设计的含义**：我们**没有遥测自动发现**，CMDB 是**唯一的拓扑来源**。所以"以 CMDB 为
主路径"对我们不是"选了业界认为更差的路"，而是**处境不同**——这一点不能被本节结论推翻。

### 8.2 召回是确定性的，且从不使用 embedding 做服务定位

Alertmanager 的分组/抑制/静默全靠 **label + 配置文件 matcher**，不含依赖图、不含拓扑、
不含服务目录；Zabbix 的全局事件关联是 **tag 配对 + 抑制**，条件只有 tag 名/值、主机组。

**向量检索在商业 AIOps 里确实有落地，但索引的语料是人写的排障文档，不是服务身份。**
Dynatrace Davis CoPilot 每 6 小时对环境共享的排障指南做向量索引，按语义相似度检索——
**目标是文档，不是 CMDB CI、不是服务实体、不是拓扑节点**。

**对本设计的含义**：

- §2.4 的 `keywords` 字面匹配**符合业界实践**，不是权宜之计
- §3.4 的**分层关键词匹配**（精确 + 模糊，不做语义）**与业界一致**——这条调研结论
  直接支持了 2026-09-15 放弃 embedding 的决定
- 代价是**没有第二道防线**：漏召只能靠 §3.4 的"召回为空 → 回退全量给 LLM"兜底，
  而那个兜底要求**目录能装进 LLM 上下文**。目录大到装不下时，这个方案会失效

### 8.3 「自由文本 → 服务身份」在样本里不存在

所有入口都是**结构化的**：工单带 CI 引用，告警带 label，直接消费。**没有任何产品文档记录了
把自由文本故障描述映射到服务身份这一步**，也没有找到服务实体本身的 embedding 索引。

唯一被采样到的 LLM SRE agent（HolmesGPT）**两次对抗验证都没过**（1-2、0-3）——
它实际是直接消费告警 label。**"LLM-based RCA"这个桶在本次调研中没有站得住的证据。**

**对本设计的含义**：§3 整节是**行业空白**。可能是真空，**也可能空白的原因是业界不需要**
（它们的服务身份从插桩和 label 免费得到）。我们做它，是因为我们没有那些来源——
这是**被迫的选择，不是主动的差异化**，设计上要按"没有先例可抄"来对待：多做防御、
多在测试上钉死。

### 8.4 根因输出形态是厂商分歧，不是共识

| 厂商 | 输出 |
|---|---|
| ServiceNow Predictive AIOps | **带置信分的候选集**（plural CIs + confidence scores + reasoning） |
| Dynatrace | **单个根因实体，不暴露置信度**。内部有 0-1 排名，但**只在 top 明显领先时才显示**（如 0.8 vs 0.01），否则干脆不显示 |

**对本设计的含义**：§3.5 的"带置信度的候选集"与 ServiceNow 同形，**站得住**。
Dynatrace 那个**置信度门控**（不明显领先就不给答案）值得借鉴——已落进 §4 的档位设计。

### 8.5 术语没有统一，且我们可能与 Dynatrace 方向相反

- `fault propagation` **在 Dynatrace 文档里 grep 零命中**——不是业界术语
- `blast radius` 是**真实在用的**
- Datadog 用**空间位置**表达（左 = 靠近客户/入口，右 = 更可能是根因），
  并建议"一次一依赖地 pivot"
- Dynatrace 把 blast radius 用于**下游受影响集**、root cause 用于**上游起源**

⚠️ **我们的 `upstream`=爆炸半径（谁调用我）、`downstream`=根因候选（我调用谁），
方向词与 Dynatrace 的因果方向可能正好相反**（该结论票数 2-1，建议复核）。
懂 Datadog/Dynatrace 的人看我们的工具输出会拧——复核后决定是否调整措辞。

### 8.6 业务域分层建模：没找到机制层面的证据

唯一相关的是一句产品表面命名（ServiceNow Service Maps 提供 "business-service-context
topology"）。**没有验证到 Journey/Portfolio/App 这类分层在运行时参与 incident→service
定位。** 这个结论置信度 **low**，是覆盖缺口而非已验证的否定。

**对本设计的含义**：§2 的四级分层**没有业界先例可抄**。它的价值主张必须自己论证
（我们论的是：让 LLM 有语义可判断），不能靠"业界这么做"背书。

---

## 9. 开放问题

1. **`keywords` 谁来写？** 这是整个设计的地基，而构建界面还没做。要不要先按现有 10 个服务的
   真实语义**起草一份 `keywords` 草稿**交人工审校？（与上一轮派生 Portfolio 同一手法——
   只用真实信息，但需人工过一遍）
2. **`expand_search` 由谁决定？** `scope` 节点自己定（LLM 判断，简单但不可控），
   还是由 workflow 的 `when` 条件边按置信度定（可控但 DAG 要加条件边与分支）？
3. **`app` 与它所在 `environment` 的关系要不要显式建模？** 若要，是一类新边
   （`deployed_in`，app → app），会与"app 不直连"的分层约束冲突——需先定这个。
4. **`support` 要不要拆成 `app_team` / `app_wiki` 两条？** 现在合并成一条 + `support_kind`，
   但 `app_codebase` 本轮已经独立成边了，四条 app 支撑关系里三条独立、一条合并，不太一致。
5. **`enterprise` 的粒度**：销售 / 售后 / 零售 / 理赔是并列的四个 enterprise，
   还是"零售"是根、其余是其下？这决定这棵树是一层还是两层。
6. **意图分类的类别集**：§3.4 暂定「故障处置 / 变更升级 / 咨询其他」三类。够不够？
   变更类里"升级 Java 版本"和"调整配置"要不要再分（前者靠属性过滤就能精确命中，
   后者可能要靠业务域）？**类别集直接决定召回策略的路由表**，是 §3.4 的地基。
7. **`upstream` / `downstream` 措辞要不要改？** 见 §8.5——我们的方向词可能与
   Dynatrace 的因果方向正好相反。改名的代价是 `get_service_topology` 的返回契约
   （12 个测试 + 可能的调用方），收益是跨产品沟通不拧。**建议先复核再定，别急着改。**
8. **层级衰减与路径叠加的具体规则**：§3.4 给的 `app→high / domain→high / portfolio→medium /
   journey→low / enterprise→极低` 以及"命中路径数叠加"**都是拍出来的**，没有数据支撑。
   需要一组真实 ticket 做校准——**这是最该先攒数据的地方**：攒 20~30 张已归档工单
   （带人工标注的真实服务），跑一遍看命中的层次分布，再回来定阈值。
   在那之前，这套规则应当**可配置**，不要硬编码进提示词。
9. **高层抽象会不会过度收敛？** §3.4 依赖 LLM 把"打印工单没反应"抽象成「工单管理」。
   但如果抽象错了（比如抬到了「售后」这种过宽的层），会**静默地召回一大堆无关服务**，
   而 `hit_paths` 还会因为命中而升高——**错误会被置信度放大而不是暴露**。需要考虑：
   抽象结果是否要**回显给人确认**，或者对过宽的抽象层做惩罚而非奖励。
