# Constellation 参考图谱解析（ontology 设计输入）

> **来源**：`multi-agent-workflow/ontology/1.png` ~ `11.png`（共 11 张 SIP 平台截图）
> **性质**：对参考界面的**忠实提取 + 推断标注**，不是设计稿，也不是最终 schema。
> **重要提示**：截图之间数字口径互相冲突（见 §7）。**不要把这些数字当作事实基线**，只把**界面呈现的类型系统**当作设计输入。
>
> ---
>
> ## ⚠️ 2026-09-15 重大更正（有硬证据，推翻本文 §2 / §3 / §7 的多处结论）
>
> 更正依据：`service-intelligence-platform-ui` 仓库里的 `tools/constellation-export/extract.py`
> 与 `tools/tenant_data.py`——它们把参考站打包在公开 JS chunk 里的**全量数据**读了出来
> （本地就有，`public/_next/static/chunks/0wuztp2.9oz~8.js`，10 个租户 4.5MB，无需联网）。
>
> | 本文原结论 | **实际** |
> |---|---|
> | 界面有 **12 类节点** | 数据里只有 **4 类**：`enterprise` / `journeys` / `portfolios` / `apps`。其余 8 类（Team/Agent/Tool/Codebase/Wiki/Incident/Change/Cross-Journey hub）**连键都不存在**——它们是**界面层的筛选分类**，不是数据 |
> | 界面有 10~13 类边 | 数据里只有 **3 类** `portfolioEdges`（`techOverlap` / `changeCascade` / `incidentCoincidence`），且都是 **portfolio ↔ portfolio**。界面上那串边名是**界面层的语义分类** |
> | 归属靠"边"（Journey link / Portfolio link） | 实际是**父指针字段**：`app.portfolioId`、`portfolio.journeyId`。**单亲树**，没有归属边 |
> | 有 `Enterprise → Journey → Portfolio → App` 四级 | ✅ 对（但**没有 `domain` 层**——那是我们自己的扩展） |
> | 截图数字口径混乱、"演示原型" | 是**多租户站点**（10 个租户）+ **一个硬编码的搜索框占位文案**。见 §7 |
> | `TOTAL NODES 352 = 282+9+6+54+1` | 算术对，但 **`54` 是 `facts.teams`——一个标量数字，不是节点集合**。界面把标量当节点数加进去了 |
>
> **结论不变的部分**：本文对**界面呈现的图例与交互**（筛选维度、灰化、1-HOP blast radius、
> 详情面板字段）的提取仍然准确——那些确实是界面在表达的东西。变的是**把它们当成数据模型**这件事。

---

## 0. 一句话结论

参考站要表达的是一张**多类型节点、具名语义边、可横切打标**的运维知识图。
它的核心价值不在"画得好看"，而在于：**把"一个事件"和"它可能牵动的应用"之间，铺成了图上的一条可遍历路径**。

---

## 1. 界面总览

| 项 | 内容 | 证据 |
|---|---|---|
| 产品 | Accenture **SIP** — Service Intelligence Platform | 1–11 顶栏 |
| 当前面板 | **Constellation**，标记为 `PANEL 1/9` | 1–11 底栏 |
| 一级导航（8 个 panel） | Constellation · Business Ops · Service Ops · Engineering Ops · Architecture · FinOps · Security Ops · Agents | 1–11 顶栏 |
| 主题域 | "The Acme Retail operational fabric" | 页头副标题 |
| 自我描述 | 282 apps across 9 portfolios + 6 journeys, the support team layer, the agent fleet, and the cross-cutting events | 页头副标题 |
| 状态 | `Live · 14m fresh` / `SNAPSHOT 2026-05-12` | 底栏 |
| 规模scope | `SCOPE 282A · 9P · 6J · 54T`，`TOTAL NODES 352` | 底栏 |

**推断（⚠️ 2026-09-15 更正）**：算术确实成立——
`352 = 282 App + 9 Portfolio + 6 Journey + 54 Team + 1 Enterprise`，且这五个数分别对应
`facts.apps` / `facts.portfolios` / `facts.journeys` / **`facts.teams`** / 1 个 enterprise。

**但**：`facts.teams` 是**一个标量数字**，数据里**没有 team 节点**（见顶部更正）。
所以 `TOTAL NODES 352` 是界面**把一个统计标量当成节点数加进去**算出来的——
**它不是"图上实际有多少个节点"**。retail 图上真实存在的节点是
`282 apps + 10 portfolios + 6 journeys + 1 enterprise = 299`（且 portfolios 列表是 10 个，
`facts.portfolios` 只数有应用的，差的那 1 个是 `portfolio:real-estate`，0 应用）。

页头的副标题本身就点明了图的四个组成部分，值得原样记住：

1. **the apps**（应用层）
2. **the portfolios + journeys**（业务组织层）
3. **the support team layer**（组织支撑层）
4. **the agent fleet**（智能体层）
5. **the cross-cutting events**（横切事件层）← 这一层是"问题→服务"的关键

---

## 2. 节点类型（界面列 12 类，**数据里只有 4 类**）

> ⚠️ **2026-09-15 更正**：下表是**界面筛选项**的忠实提取，**不是数据模型**。
> 参考站打包的数据里只有 **4 类节点集合**：`enterprise`（1 个）· `journeys` · `portfolios` ·
> `apps`。第 6–12 行（Team / Agent / Tool / Codebase / Wiki / Incident / Change）以及
> **Cross-Journey hub**，在数据里**连键都不存在**——勾选它们图上不会有任何节点出现。
>
> 教训：**界面上的筛选分类 ≠ 数据模型**。把前者当后者，会照着做一个数据里根本没有的 schema。

面板计数 `NODE TYPE  n of 12`，与拼合三张截图的列表长度一致。

| # | 类型 | 圆点色（近似） | 视角 | 语义 |
|---|---|---|---|---|
| 1 | **Enterprise** | 紫 | 业务 | 全局根节点，整个 fabric 的锚点（推断：即 TOTAL NODES 里多出的那 1 个） |
| 2 | **Journey** | 蓝 | 业务 | 用户/业务旅程，跨多个 Portfolio 的能力主线 |
| 3 | **Portfolio** | 蓝 | 业务 | 业务域（Stores / E-commerce / Supply Chain …），应用的归属分组 |
| 4 | **Cross-Journey hub** | 灰 | 业务 | 被多条 Journey 共用的枢纽（推断：跨旅程的共享能力/共享应用集合） |
| 5 | **App** | 深灰/近黑 | 应用 | 应用系统，图的主体（数量最多） |
| 6 | **Team** | 黄/琥珀 | 组织 | 支撑团队，负责某些 App |
| 7 | **Agent** | 橙 | 组织 | 智能体，运维自动化单元 |
| 8 | **Tool** | 紫 | 组织 | 工具，被 Agent 集成调用 |
| 9 | **Codebase** | 绿 | 工程资产 | 代码仓库 |
| 10 | **Wiki** | 紫 | 工程资产 | 文档/知识页 |
| 11 | **Incident** | 红 | 运营事件 | 故障事件 |
| 12 | **Change** | 品红/粉 | 运营事件 | 变更事件 |

> 颜色是从筛选列表左侧的小圆点读取的，尺寸很小，**建议后续复核**。真正要确认的是上面的**类型语义与层次**，而非色值。

**证据拼合**：
- `1.png` 列出前 7 个（Enterprise → Agent）
- `2.png` 下滑一位，露出 **Codebase**
- `11.png` 滑到底，露出尾部 7 个（Team → Tool），补齐 **Wiki / Incident / Change / Tool**

---

## 3. 边类型（界面列 10~13 类，**数据里只有 3 类**）

> ⚠️ **2026-09-15 更正 —— 这一整节建立在错误前提上。**
>
> 数据里只有 **3 类边**，全部是 **portfolio ↔ portfolio**，且**不带方向语义**：
>
> | 键 | 含义 | retail 条数 |
> |---|---|---|
> | `techOverlap` | 技术栈重叠（`{source, target, sharedTechs}`） | 19 |
> | `changeCascade` | 变更级联（`{source, target, pairCount}`） | 17 |
> | `incidentCoincidence` | 事件共现（`{source, target, weight, pairCount}`） | 17 |
>
> 下面这张表里的 `Journey link` / `Portfolio link` / `Support·team+wiki` / `Agent watches` /
> `Tool integrates` / `Storm fan-out` / `Change → incident` / `Incident cluster → app` /
> `Change cluster → app` —— 这些是**界面层的语义分类标签**（多半由归属字段和单位数据派生出来
> 用于展示），**不是数据里的边**。而且 `app` 的归属（`portfolioId`）与 `portfolio` 的归属
> （`journeyId`）是**父指针字段**，压根不是边。
>
> 所以"`All 13` vs `1 of 10` 差了 3 条"这个疑问**不成立**——它本来就不是数据里的边类型计数。
> 原文保留在下方，仅作为"界面在表达什么"的记录。

**这是全套截图里最需要澄清的一处。** 见 §7。

`7.png` / `9.png` / `10.png` / `11.png` 的面板显示 `EDGE TYPE  1 of 10`，列表共 10 项；
但 `1.png` 显示 `EDGE TYPE  All 13`。**两者相差 3 条，且这 3 条在任何截图里都没露出来。**

以下 10 条是**实际看到名字**的：

| # | 边名 | 线色（近似） | 方向性 | 语义 | 推断端点 |
|---|---|---|---|---|---|
| 1 | **Journey link** | 蓝 | 无向 | 旅程 ↔ 业务域 的归属 | Journey — Portfolio |
| 2 | **Portfolio link** | 蓝 | 无向 | 业务域 ↔ 应用 的归属 | Portfolio — App |
| 3 | **Cross-journey link** | 灰 | 无向 | 跨旅程共享关系 | Journey — Cross-Journey hub |
| 4 | **Support · team + wiki** | 琥珀 | 无向 | 支撑关系，一个名字覆盖两类支撑物 | App — Team，App — Wiki |
| 5 | **Agent watches** | 橙 | 有向（watch） | 智能体在监视什么 | Agent → App（或 Agent → Incident） |
| 6 | **Tool integrates** | 紫 | 有向（integrate） | 工具集成关系 | Tool → App / Agent |
| 7 | **Storm fan-out** | 红 | 有向（扇出） | 故障风暴：一个源点扇出到一大片 | Incident → Incident（一对多） |
| 8 | **Change → incident** | 红/粉 | 有向 | 变更引发故障 | Change → Incident |
| 9 | **Incident cluster → app** | 红 | 有向 | 故障簇落点到应用 | Incident(cluster) → App |
| 10 | **Change cluster → app** | 粉 | 有向 | 变更簇落点到应用 | Change(cluster) → App |

**几个值得注意的观察**：

- **具名语义，不是泛"关联"**。每条边都是一个具体动词（link / support / watches / integrates / fan-out / causes）。这正是 ontology 与普通拓扑图的区别。
- **`Support · team + wiki` 把两类支撑物压进了一条边**（团队 + 文档）。设计上是有意的省并，实现时要决定是拆成两条还是保留为一条带 `subtype` 的边。
- **`Incident cluster` / `Change cluster` 作为边的一端出现，但不在 12 类节点里**。这暗示"簇"要么是 Incident/Change 节点的一种聚合形态，要么是第 13 类被省略的节点。**这是一个真实的开放问题**（见 §9）。
- **第 8/9/10 条边全部指向 App**。这三条就是"事件 → 候选服务"的答案载体（见 §8）。
- **`Storm fan-out` 是故障扇出，不是服务调用依赖**。参考站里**没有**看到"服务调用/依赖"这一类边——这是它与我们现有 CMDB 最大的差异（见 §8）。

---

## 4. 横切属性（KEY ATTRIBUTES，7 个）

面板计数 `KEY ATTRIBUTES  n of 7`。这类标签**可跨节点类型打标**——同一个 `Tier 1` 可以打在 App 上，也可以打在 Portfolio 或 Journey 上。

| # | 属性 | 圆点色（近似） | 含义 | 类型 |
|---|---|---|---|---|
| 1 | **Tier 1** | 红 | 一级系统 | 分级标签（静态） |
| 2 | **Holiday-critical** | 黄 | 节假日关键 | 时效标签（静态） |
| 3 | **Finance-freeze** | 紫 | 财务冻结期 | 时效标签（静态） |
| 4 | **Manhattan WMS spotlight** | 黄 | 特定系统的关注焦点 | 专题标签（静态） |
| 5 | **Top 10 by incidents · 18M** | 红 | 近 18 个月故障数 Top 10 | **算法派生**（窗口 18M） |
| 6 | **Top 10 by changes · 18M** | 粉 | 近 18 个月变更数 Top 10 | **算法派生**（窗口 18M） |
| 7 | **Emergency changes** | 橙 | 紧急变更 | 事件派生 |

**重要区分**：第 1–4 条是**人工声明的静态标签**；第 5–7 条是**从数据算出来的动态标签**（注意 5/6 带时间窗口 `18M`）。
设计 ontology 时这两类必须分开——前者是属性，后者是**视图/指标**，会随时间变化，不适合作为静态文件里的字段。

`Tier 1 · Holiday` 同时被用作 **SAVED VIEW** 的名字，说明属性是可以组合成预设视图的。

---

## 5. 筛选与交互模型

### 5.1 四个筛选维度（可叠加）

| 维度 | 控制项 | 计数语义 |
|---|---|---|
| **NODE TYPE** | 12 类复选 + 搜索框 | `n of 12` |
| **PORTFOLIO** | 业务域复选 + 搜索框 | `n of 22`（另见冲突） |
| **KEY ATTRIBUTES** | 7 类复选 | `n of 7` |
| **EDGE TYPE** | 10 类复选 + 搜索框 | `n of 10`（另见冲突） |

面板顶部显示 `FILTER n active` 与 `CLEAR` 按钮。

### 5.2 命中/未命中 = 高亮/灰化

筛选不是"隐藏"，而是"**去饱和**"：不匹配的节点和边变成灰色，**仍留在图上**。
从 `3.png` → `4.png` 可以看到：选中 Supply Chain + Customer Care 两个 Portfolio 后，图里只剩对应簇是彩色的。

这个交互本身就是一种**探索式查询**：用户通过"调筛选器 + 看剩下什么"来理解图谱结构。

### 5.3 SAVED VIEWS（4 个预设）

| 名称 | 推断含义 |
|---|---|
| **Reset** | 清空所有筛选 |
| **WMS** | 聚焦 WMS 相关（对应 `Manhattan WMS spotlight` 属性） |
| **Tier 1 · Holiday** | 一级系统 + 节假日关键（对应属性组合） |
| **Storm fan-out** | 故障风暴视图（对应 Storm fan-out 边 + 事件节点） |

### 5.4 其它交互

- **搜索**：`Search 893 apps · 22 portfolios · 6 journeys · 156 agents`，快捷键 `⌘K`
- **节点详情**：点击节点 → 右侧滑出详情面板
- **1-HOP BLAST RADIUS**：详情面板里每个节点都有一个 `1-HOP` 徽章，表示**以该节点为中心、一跳邻居构成的爆炸半径**。这是一个可切换的聚焦模式
- **图例**：左下 `LEGEND` 按钮
- **画布控件**：右侧 放大 / 缩小 / 适应 / 收起

---

## 6. 节点详情面板的字段

三类节点各看到了一个真实实例，字段如下：

### 6.1 App（`1.png`）

```
APPLICATION · TIER 1                      [1-HOP] [BLAST RADIUS]
POS Direct DR
─────────────────────────────────────────
[T1]  Risk 94.00                          ← 徽章 + 风险分
INCIDENTS · 18M                     248
BOT RESOLUTION                      54%
CHANGES · 13M                        45
```

### 6.2 App · Spotlight（`5.png`）

```
APPLICATION · TIER 1 · SPOTLIGHT          [1-HOP] [BLAST RADIUS]
WMS Direct UAT 2
─────────────────────────────────────────
[T1] [HC]  Risk 64.00                     ← HC = Holiday-critical
INCIDENTS · 18M                      76
BOT RESOLUTION                       64%
CHANGES · 13M                        13
```

### 6.3 Portfolio（`3.png`）

```
PORTFOLIO                                 [1-HOP] [BLAST RADIUS]
Supply Chain
─────────────────────────────────────────
APPS                                 36
INCIDENTS · 18M                   5,161
CHANGES · 13M                       924
```

### 6.4 Journey（`8.png`）

```
JOURNEY                                   [1-HOP] [BLAST RADIUS]
Engage
─────────────────────────────────────────
CAPABILITY                        Engage
PORTFOLIOS                             1
APPS                                  25
```

**观察**：面板标题行是**动态的、可组合的**——`APPLICATION · TIER 1 · SPOTLIGHT` 由"节点类型 + 命中的属性标签"拼成。说明属性在 UI 上是与应用类型并列的一等公民。

**观察**：App 有 `Risk` 分，Portfolio/Journey 没有。风险分是 App 级的概念。

**观察**：指标都带**时间窗口**（`18M` / `13M`），且窗口在不同指标上**不一样**（故障 18 个月、变更 13 个月）。设计时时间窗口必须是数据的一部分，不能写死。

---

## 7. 截图间的口径冲突 —— **已解释清楚**（2026-09-15 重写）

> 原文的判断是"该界面是演示原型，数字来自不同的硬编码快照"。**结论方向对（数字不可采信），
> 但原因是错的**，而且错得有意义。实际是一句话：
>
> **这是个多租户站点，而搜索框的占位文案是硬编码的。**

### 7.1 硬证据

`Search 893 apps · 22 portfolios · 6 journeys · 156 agents` 是**写死在 JS 里的字面量**：

```
$ grep -o "Search [0-9]* apps[^\"']\{0,80\}" public/_next/static/chunks/0p_dozb5cvong.js
Search 893 apps · 22 portfolios · 6 journeys · 156 agents …
```

而它写的是 **`best-buy-reference`** 租户的数（893 apps / 22 portfolios）。站点当前激活的租户
是 **`retail`**（282 apps / 10 portfolios）——**页面主体显示 retail，搜索框写着 best-buy 的数**。

### 7.2 逐个解释

| 项 | 值 A | 值 B | **实际原因** |
|---|---|---|---|
| 应用数 | 282（页头） | 893（搜索框） | 页头是**当前租户 retail**；搜索框是**硬编码**的 best-buy 数字 |
| Portfolio 数 | 9（页头/scope） | 22（搜索框） | 同上。**且 9 本身也没错**——`facts.portfolios` 只数**有应用的**组合，列表里还有 1 个 `portfolio:real-estate`（0 应用），所以列表是 10 |
| Portfolio 列表 | `All 22` | `2 of 10` | `All 22` 来自同一个硬编码区间；`2 of 10` 是 retail 的真实列表 |
| EDGE TYPE | `All 13` | `1 of 10` | **都不是数据里的边**——数据里只有 3 类 `portfolioEdges`。这串名字是界面层的语义分类（见 §3） |
| Agent 数 | 156（搜索框） | 0 online（底栏） | 前者是硬编码；`snapshot.agents` 的三个桶（`itil-run`/`sdlc`/`governance`）**全为空数组** |
| TOTAL NODES | 352 | — | 算术成立但口径错：`54` 是 `facts.teams` 标量，不是节点（见 §1） |

### 7.3 那到底什么可以采信

| | 采信 | 说明 |
|---|---|---|
| ✅ | **界面交互模型** | 四维筛选、灰化而非隐藏、1-HOP blast radius、详情面板字段结构——这些确实是界面在表达的 |
| ✅ | **`retail` 租户的真实数据** | 282 apps / 10 portfolios / 6 journeys / 3 类 portfolio 边——**可复现**（本地 bundle 直接读） |
| ❌ | **界面上的任何数字** | 搜索框硬编码、TOTAL NODES 口径错、边类型数是界面概念 |
| ❌ | **把 12 类 / 10 类当成数据模型** | 数据里只有 4 类节点、3 类边（见 §2 / §3） |

### 7.4 retail 租户的真实基线（可复现）

```
enterprise  1 个     {id, label:"Acme Retail", position}
journeys    6 个     Browse / Buy / Pick / Ship / Return / Engage（各有 capability）
portfolios 10 个     各带 {id, label, tier(1-3), journeyId, position}
apps      282 个     各带 {id, appKey, label, portfolioId, tier(1-3), holidayCritical,
                          financeFreeze, operationalStatus, incidentsTotal, changeCount,
                          riskScore, botResolutionRate, botMttrMin, humanMttrMin, position}
portfolioEdges  3 类（techOverlap 19 / changeCascade 17 / incidentCoincidence 17）
agents            {itil-run:[], sdlc:[], governance:[]}   ← 全空
```

**归属是父指针字段，不是边**：`app.portfolioId` → 单亲；`portfolio.journeyId` → 单亲。

---

## 8. 对我们自建 CMDB 实体文件的启示

> 本节是我的分析，不是截图内容。写在这里是为了让后续设计有落点。

> **✅ 本节已落地（2026-09-15）**，实现位于 `aiops-mcp-servers/servers/aiops-datasource-mcp-server`：
> 实体文件 `src/.../data/cmdb-entities.json` · 加载器 `backends/entity_graph.py` ·
> 图查询与推断 `backends/graph_query.py` · schema 参考 `docs/cmdb-entities.md`。
> 对外契约零改动（`get_service_topology` / `locate_repo` 改动前后输出逐字节相同）。
>
> **落地时与本节有三处差异**，都是实现阶段才暴露的：
>
> 1. **没有发明 App→Codebase 边**，改用受校验的 `refs.repo_ref` 引用字段。
>    ⚠️ 当时给的理由（"它可能是 §7 那个 `All 13` vs `1 of 10` 之谜里缺失的边之一"）
>    **已作废**——那个"谜"不存在（§3 / §7.2）。真实原因是：**数据里根本没有 codebase 这个概念**，
>    所以无从"照搬"。**不过结论仍然成立**，而且后来（v5.7）还是把它提升成了 `app_codebase` 边——
>    那次是**用户明确要求**"app 可以关联 codebase"，不是从参考站推的。
> 2. **新增的是 `calls`（11 类中的第 11 类）**，且 Incident / Change 走**独立可选覆盖层文件**，
>    不写进主实体文件——事件是带时间窗口的观测数据，主文件装的是人工维护的拓扑。
>    理由与 §4 的"静态 vs 派生必须分开"是同一条。
> 3. **候选推断把「证据强度」与「影响面」拆成两个轴**（`confidence` / `impact`），
>    而不是把 `tier1` 折进置信度——否则一个仅凭拓扑相邻的候选会拿到与"调用方已确认的
>    症状服务"同等的置信标签，那是把重要性冒充成可能性。

### 8.1 现状与参考的差距

我们现有资产（`aiops-mcp-servers` 的 CMDB/拓扑）大致只有：

- **App** 节点（testbed 3 个服务 / mock 10 个服务）
- **App —CALLS→ App** 的调用边（约 13 条，带 `http/rpc/mq`）

> ⚠️ **2026-09-15 更正**：下表"参考图里对应"那一列写的是**界面筛选项**，不是数据模型。
> 数据里只有 4 类节点（enterprise/journey/portfolio/app）和 3 类 portfolio 边——
> **下表中的 Team / Agent / Tool / Codebase / Wiki / Incident / Change / Cross-Journey hub
> 在参考数据里一概不存在**。所以这张表**只能读作"界面在表达哪些概念"**，
> 不能读作"参考站有这些数据、所以我们也该有"。
>
> 表里**唯一仍然成立**的对比是"事件层"那一行：参考站把 `incidentsTotal` / `changeCount`
> 做成 App 上的**统计数字**，确实没有 Incident/Change **节点**。

对照界面表达的 12 类节点、10 类边，**我们缺的是**：

| 缺什么 | 参考图里对应 | 对我们的价值 |
|---|---|---|
| 业务组织层 | Enterprise / Journey / Portfolio / Cross-Journey hub | 让"业务影响面"可表达 |
| 组织层 | Team / Agent / Tool | 让"找谁处理"可表达 |
| 工程资产层 | Codebase / Wiki | 让"去哪看代码/文档"可表达（我们已有 repo 映射，可归到 Codebase） |
| **事件层** | Incident / Change | **最关键的缺口**——没有事件节点，"问题→服务"就只能靠关键词匹配 |
| 事件→应用的边 | Incident cluster → app 等 | 候选服务推断的载体 |

### 8.2 我们的调用关系往哪放

参考图里**没有**服务调用边（`Storm fan-out` 是故障扇出，不是调用依赖）。
所以我们的 `CALLS` 边**不是照搬，而是新增**——它是参考图缺、但我们有且不能丢的一类边。

建议：新增为第 11 类边 **`App → App`（调用依赖）**，保留 `http/rpc/mq` 作为边属性。

### 8.3 "问题描述 → 候选服务"的路径

参考图给的答案是：**不要做关键词匹配，做图上遍历**。

```
问题描述
   │  (匹配或新建)
   ▼
Incident 节点
   │  Incident cluster → app
   ▼
App 集合（候选服务）      ←── 也可经 App —CALLS→ App 做一跳拓扑扩展
```

我们已确认的四个决策（上一轮）：

1. **规模**：照搬 12 节点 + 10 边的 schema 形状，用我们的数据填
2. **调用关系**：新增为一类 App → App 的边
3. **路径**：先匹配或新建 Incident 节点，再沿"事件→应用"边落到 App
4. **本次范围**：实体文件 + 图查询 + 候选推断 + 接回诊断链

### 8.4 横切属性的处理

参考图的 7 个属性要**拆成两类**（见 §4）：

- **静态属性**（Tier 1 / Holiday-critical / Finance-freeze / …）→ 写进实体文件，作为节点字段
- **动态指标**（Top 10 by incidents · 18M / Top 10 by changes · 18M / Emergency changes）→ **不要写进静态文件**，应由查询时计算

---

## 9. 待确认的开放问题

1. **缺失的 3 条边是什么？** `All 13` vs 列表 10 项。是原型遗留，还是确实有 3 类没截到？**这是照搬 schema 前必须回答的第一个问题。**
2. **Incident cluster / Change cluster 是节点还是聚合？** 它们作为边的一端出现，却不在 12 类节点列表里。
3. **`Support · team + wiki` 要不要拆成两条边**（App—Team、App—Wiki）？合并省事，拆开更清晰。
4. **Portfolio 到底是 9 / 10 / 22 个？** 三者都出现过。我们自己的实体文件里定几个？
5. **Cross-Journey hub 的精确定义**是什么？它是"被多条 Journey 共享的应用集合"还是独立的节点？
6. **`Risk` 分怎么算？** 参考图里只有 App 有。我们的实体文件里要不要有这个字段（如果要有，是静态值还是算出来的）？
7. **时间窗口**（18M / 13M）在不同指标上不一致——是我们的实体文件也要带，还是统一成一个？

---

## 附：截图索引

| 文件 | 内容 |
|---|---|
| `1.png` | 全景 + 节点详情（POS Direct DR）+ 筛选面板（2 of 12） |
| `2.png` | NODE TYPE 下滑，露出 Codebase（4 of 12） |
| `3.png` | PORTFOLIO 列表展开 + Portfolio 详情（Supply Chain） |
| `4.png` | 同 3，无右侧面板 |
| `5.png` | KEY ATTRIBUTES 全 7 项展开 + App 详情（WMS Direct UAT 2） |
| `6.png` | 同 5，无右侧面板 |
| `7.png` | **EDGE TYPE 面板前 7 项** + Journey link 选中 |
| `8.png` | Journey 详情（Engage） |
| `9.png` | **EDGE TYPE 面板后 7 项**（放大图） |
| `10.png` | PORTFOLIO 列表下滑（Merchandising → Real Estate） |
| `11.png` | **NODE TYPE 尾部 7 类**（Team → Tool） |
