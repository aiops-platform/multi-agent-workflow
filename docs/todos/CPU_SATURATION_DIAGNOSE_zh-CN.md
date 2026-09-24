# 指标驱动的 CPU 饱和诊断：现状调研与改造清单

> 状态：**调研稿 / 待决策**（2026-09-24）。**未动任何代码**，本文只有结论与清单。
> 读者：**你**，在决定"这块怎么优化"的时刻读它。§7 是待你拍板的分叉，其余各节是决策依据。
> 触发场景：监控发现 **CPU 持续 1–2 分钟居高不下**，希望平台给出根因分析与修复计划。
>
> 关联：`docs/design-v5.8.md` §3.3（数据工具规格）、§3.4（CMDB 本体）；
> `scripts/problem-log-diagnose.workflow.yaml`（现有诊断链）、
> `agentflow/seed/workflows/problem-diagnose-fix.yaml`（现有修复链）、
> `docs/TODO.md` §20（定位失败的静默通过）。

---

## 0. 结论摘要（tl;dr）

1. **不需要新增 agent。** 干这活的四个（`metrics-analyst` / `infra-locator` / `code-locator` /
   `root-cause`）+ 方案侧的 `remediation-planning-analyst` **已经在编队里**，且
   `problem-log-diagnose → create-ticket(next_workflow) → problem-diagnose-fix` 这条
   诊断转修复的机制**已经通了**。
2. **缺的是三样，分别在三个地方**：
   - **触发入口**（平台侧完全没有）—— 见 §2.2；
   - **"持续"这个时间语义的承载能力**（schema 装不下）—— 见 §3.1；
   - **把指标当主证据而非旁证的诊断链**（现有 prompt 明确禁止追问）—— 见 §3.3。
3. **按现状直接跑，会得到一份"看着合理、实则站不住"的根因。** 四条理由在 §3，
   每条都有代码位置；其中两条（`value` 取峰值、服务级聚合平均掉单副本）会让
   **尖刺被报成饱和**、**单副本跑飞被平均掉**。
4. **"持续 1–2 分钟"这个判据本身建议留在监控系统**（Prometheus 规则 / Alertmanager），
   agent 只负责解释与追问 —— 理由见 §7.3。
5. **本文所有事实一手核实**（读源码 + grep，非推测）。核实方式见 §8。

---

## 1. 现状：这活的四个 agent 已在编队里

`agentflow/agents/registry.py:25-45` 的 9 个诊断 agent 中，CPU 高根因只需要四个：

| agent | 它回答的问题 | 工具 | 一句话判据 |
|---|---|---|---|
| `metrics-analyst` | CPU 有多高、哪些指标一起动了 | `query_metrics` | **本文改造重点** |
| `infra-locator` | pod 状态 / 重启次数 / 事件 / 资源水位 | `check_infra` / `describe_pod`（**无时间参数**，查当前状态） | 回答"现在什么样" |
| `code-locator` | 服务 → 仓库 + 可疑文件 | `locate_repo` / `get_service_topology` | 单副本跑飞常是代码问题 |
| `root-cause` | 交叉核对给 `root_cause_type` + 置信度 | 全数据源 + `search_code` / `blame_file` | 收口 |

接方案：`remediation-planning-analyst`（给人看的方案，`problem-log-diagnose` 的 `plan`
节点在用）或 `fix-planner`（可执行步骤 `code_fix` / `infra_action` / `config_change`）。

**这四个正好是 `scripts/problem-log-diagnose.workflow.yaml` 那条链的形状** ——
把 `logs` 节点换成 `metrics` 节点即可，不需要新的 DAG 骨架。

**下游修复段已通**：诊断链末尾的 `create-ticket` 节点带
`next_workflow: "problem-diagnose-fix"`（`problem-log-diagnose.workflow.yaml:211`），
建单 + 钉流程是一次原子决策，修复段会自己接上。

---

## 2. 现状：入口与取数契约

### 2.1 触发入口：**没有**

平台是**被动执行**的，run 只能由外部主动调 API 启动：

| 入口 | 位置 | 说明 |
|---|---|---|
| `POST /run` | `agentflow/api/app.py:454` | 直接跑一条 workflow |
| `POST /tickets` | `agentflow/api/app.py:833` | 建工单（此时可钉 `workflow_id`） |
| `POST /tickets/{tid}/run` | `agentflow/api/app.py:963` | 工单发起 run |

**不存在** webhook / alert ingestion / cron 巡检 / Alertmanager 接入：

- 全部 36 条路由里没有 `/webhook`、`/alerts`、`/events` 之类；
- 队列 `run.trigger` 的**唯一发布者**是 `RunService._create`
  （`agentflow/service.py:218`，即"HTTP 已经建好 run 行之后"才发）；
- 唯一的周期循环是审批超时 Sweeper（`agentflow/approval/sweeper.py` 的 `run_forever()`，
  由 `app.py:379` 拉起）—— 它只做"扫 `WAITING_APPROVAL` → 超时 CAS → 发 resume"，
  **不建 run、不巡检指标**；
- `agentflow/datasource/prometheus.py` 只被 `GET /app-indicators`（`app.py:2018`）
  用来给**遗留前端 Smart Inspection 页**做指标快照，**从不创建 run**。

⚠️ 另有一条**出站**缺口：`approval/notifier.py` 自述"通知渠道为占位接口"，
邮件/Slack/webhook 至少一种仍是 `docs/TODO.md` 里未做的 M6 项。那是通知，不是入站告警。

### 2.2 `query_metrics` 的真实契约

真源在**隔壁仓** `aiops-mcp-servers/servers/aiops-datasource-mcp-server`
（本仓 `scripts/mock_mcp_server.py` 只有 `get_weather` / `query.repo` / `send_alert`，与指标无关）。

签名（`src/aiops_datasource_mcp_server/tools/datasource.py:232-241`）：

```
query_metrics(service, metric, start_time, end_time, step_seconds=30)
  metric ∈ {cpu_percent, memory_percent, disk_percent, error_rate, p95_latency_ms}
  step_seconds ∈ [5, 3600]，默认 30
```

返回（`backends/prometheus.py:165-178`）：窗口回显 + `value`/`min`/`max`/`avg`/`last`
+ `series_count` + **降采样 `series`（≤120 个带时间戳的点）** + `summary`。

指标到 PromQL 的映射在 `backends/prometheus.py:36-70`。**四条与本次场景直接相关的事实**：

| # | 事实 | 位置 | 后果 |
|---|---|---|---|
| 1 | `value` 默认取 **max（峰值）** | `prometheus.py:91` | 链路**天然倾向于把尖刺报成饱和** |
| 2 | `cpu_percent` = `100 * sum(rate(cpu)) / sum(quota/period)`，**对全部 pod 求和后相除** | `prometheus.py:29,43-47` | 服务级加权平均 ⇒ **单副本跑飞被平均掉** |
| 3 | `rate()` 窗口 = `max(step*2, 60)s`，**60 秒地板** | `prometheus.py:115` | **结构上分辨不了短于 1 分钟的形态** |
| 4 | 可用指标**只有 5 个**，无 `throttled` / 无 limit 本身 | `prometheus.py:81-83` | "正在被限流"与"limit 设小了"都**查不到** |

细节补充：

- **fact 2 的算术**：3 副本里 1 个打满 90%、2 个闲着 5% → 服务级显示 **~33%，不告警**；
  反之告警说 CPU 90% 时，也可能是**所有副本都 90%**（真负载）。
  两种情况的处置完全相反（改代码 vs 扩容），**而当前工具分不出来**。
- **`series_count` 有这个信息**（返回了几条序列），但没进 agent 的 schema（见 §3.1）。
- **fact 4 的一个反直觉点**：`cpu_percent` 的分母是容器 CPU limit，
  所以"CPU 90%"= **贴近 limit** = 有被限流的风险；而"没设 limit"会导致分母为 0
  被过滤 ⇒ **返回无数据**。工具契约说"不要当成 0"（正确），但**没把它当成一个信号**
  —— 而"没设 limit"本身就是 CPU 相关根因的候选之一。
- **`cpu_percent` 无数据**的三种原因工具会提示：未设 limit / 应用未暴露该指标 / 窗口内无采集点。

### 2.3 K8s 侧能查到什么

`check_infra(namespace, pod=None)`（`backends/k8s.py:105-124`）返回**逐 pod** 的
`{name, status(phase), restarts, ...}`：

- `pod` 可传**服务名**（按 `app` 标签匹配）或不传（列该 namespace 全部）；
- ⇒ **副本数可以从 pod 列表数出来**，但**没有一个字段直接是副本数**；
- ⇒ **HPA 配置完全查不到**。

`describe_pod(pod, namespace)` 需要**完整 pod 名**（含 hash 与随机后缀），
是先 `check_infra` 拿名字再调的。它是 `kubectl describe` 的薄封装，
原文里**有 Limits/Requests**，但落到 `InfraEvidenceSchema`
（`agentflow/agents/schemas.py:191-202`）只剩一个自由形态的 `resource_usage` object。

### 2.4 变更 / 事件关联：**能力是空的**

- CMDB 本体**已声明**事件节点：`entity_graph.py:89` 写着
  `event —— 事件落点（incident / change）`，且有 incidents overlay 的加载机制
  （`load_incidents_overlay()`，路径由 `datasource_incidents_path` 配置，**可选**）。
- 但当前数据文件 `data/cmdb-entities.json` 里 **`change` = 0 个、`incident` = 0 个**
  （n 节点：enterprise 1 / journey 2 / portfolio 11 / domain 1 / app 59 / team 0 /
  agent 17 / tool 0 / codebase 10 / wiki 0 / incident 0 / change 0）。
- ⇒ "CPU 突增是不是因为昨天那个发布" **今天问不出来**。

---

## 3. 为什么现有链路跑 CPU 高会得出错根因

四条理由，都在代码里。

### 3.1 "持续"这个语义在输出契约里不存在

`agentflow/agents/schemas.py:177-189`：

```python
MetricsEvidenceSchema = {
    "properties": {
        "cpu_percent": ..., "memory_percent": ..., "disk_percent": ...,
        "error_rate": ..., "p95_latency_ms": ...,
        "anomalies": [...], "summary": ...,
    },
    "required": ["anomalies", "summary"],
}
```

五个**标量**，没有时间维度。而 MCP 那边**已经返回了** `series`（≤120 个点）。
**峰值 90% 的 1 秒尖刺和 90% 的 3 分钟平台，在这个 schema 里长得一模一样。**
叠加 §2.2 的 fact 1（`value` 默认取 max），链路会把尖刺报成饱和。

### 3.2 根因节点收到的是字符串，不是数据

- `agentflow/seed/workflows/scenario2-bug-fix.yaml:202`：
  `metrics: "$.nodes.metrics.output.summary"`（scenario1 同形，`:213`）。
- 生产用的 `problem-log-diagnose` 里更彻底 —— rca 的 metrics 入参**是写死的负证据**：
  `scripts/problem-log-diagnose.workflow.yaml:110`：
  `metrics: "本次未采集（log-only 流程）"`。

⇒ **"持续 1–2 分钟"这个前提从未到达根因节点。**
（`root-cause` 自带全部数据工具会自己重查一遍，所以结论"看着仍合理"——
这与 `scenario2-bug-fix.yaml:178-193` 记录的 `join: any` 那次实测是同一种失效形态。）

### 3.3 提示词明确禁止追问

`agentflow/agents/prompts.py:227`：

> 「五个指标各调用一次即够；不要反复试不同 service/uri 做开放式探索
> —— 那会耗尽轮次导致整个节点无输出」

这是为**工单驱动**场景写的（指标只是五路旁证之一，`_MAX_ITERS` 默认 10 轮）。
但 CPU 高是**指标驱动**场景，指标就是**主证据**，需要的恰恰是追问：

- 是所有副本都高还是单副本？（`series_count` 有，没进 schema）
- 对比基线：昨天同时段高不高？（没有这个概念）
- 伴随信号：`error_rate` / `p95_latency_ms` 动了吗？重启动了吗？

**现在的 prompt 会把这些全劝退。**

### 3.4 `step_seconds` 交给模型掷骰子

`prompts.py:214-231` 的 metrics-analyst prompt 里写的调用形态是
`query_metrics(service, metric, start_time, end_time)` —— **压根没提 `step_seconds`**。
模型可能传 30（默认）、也可能不传。而**步长直接决定它看见什么曲线**
（叠加 fact 3 的 60 秒 rate 地板）。

---

## 4. 改造清单

### 4.1 必须改（否则根因不成立）

| # | 改动 | 位置 | 说明 |
|---|---|---|---|
| M1 | `MetricsEvidenceSchema` 加时间形状字段 | `agentflow/agents/schemas.py:177-189` | `sustained`（是否平台期 / 持续多久）、`trend`（窗口内走势）、`series_count`（几个 pod 在贡献）、基线对比 |
| M2 | `metrics-analyst` prompt 改为指标优先 | `agentflow/agents/prompts.py:214-231` | 从"5 个各查一次"改成"**定时序看形状 + 拉一条基线 + 报 pod 分布**"，并把 `step_seconds` 写进入参契约 |
| M3 | `rca` 入参把 `metrics.output.summary` 换成**整个 output** | 各 workflow YAML 的 rca 节点 | 一行 YAML，但这是"根因看不见指标细节"的直接原因 |
| M4 | 诊断链里 `metrics` / `infra` 节点必须 `join: all` + `required_edges` 列全直接上游 | 新 workflow | ⚠️ 本仓在 `rca` 上踩过这个坑，见 `scenario2-bug-fix.yaml:178-193` 的实测记录（同波启动 ⇒ params 全 None） |

### 4.2 建议改（决定根因质量）

| # | 改动 | 位置 | 说明 |
|---|---|---|---|
| S1 | 加指标：**限流比** `container_cpu_cfs_throttled_periods_total` | ⚠️ **隔壁仓** `aiops-mcp-servers`：`available_metrics()` + `build_query()` | 那才是用户可感知的延迟抖的直接原因；"CPU 贴着 limit"只是风险 |
| S2 | 加指标：**CPU limit 本身** | 同上 | 现在的 5 个指标里没有配额，"limit 设小了"只能靠 agent 从 `describe_pod` 自由文本里读 |
| S3 | `query_metrics` 支持**按 pod 分解** | 同上 | 解掉 §2.2 fact 2 的"单副本被平均掉" |
| S4 | `InfraEvidenceSchema` 加 `limits` / `replicas` | `agentflow/agents/schemas.py:191-202` | 现在只剩一个自由 object |

> ⚠️ S1–S3 在**另一个仓**（`aiops-mcp-servers`），改完需要重启 MCP datasource
> （本机 8300，**必须在它自己目录启动**）。另注意：改 prompt 或 schema 之后
> **必须重启 API 与 worker**，否则静默跑旧 prompt —— 这是本仓记录过的教训。

### 4.3 需要你拍板的分叉

**`problem-log-diagnose` 没有"根因类型 → 下游路径"的路由。**

- 它只有 `diagnose-output` 一个门（`problem-log-diagnose.workflow.yaml:158-168`）+ `create-ticket`；
- 之后 `problem-diagnose-fix` 是**全线性**的：`plan → approve-plan → fix → remediate → test → review → approve-commit → commit → ticket-done → recap`（`fix → remediate` 串行）；
- 而 `scenario1` 才是 `remediate` 与 `fix` **并行**的形状（另有独立的 `approve-remediate` 门）。

⇒ **CPU 高有相当比例根本不是 bug**（业务涨了 / 副本不够 / limit 设小了），
那时候 `fix-implementer` 会硬凑一个 `code_fix` 出来。两个选项：

- **(a)** 在 `diagnose-output` 门那里加**人工裁定分叉**（人的判断成本最低，改动最小）；
- **(b)** 按 `RootCauseSchema.root_cause_type`（`infra_issue` / `code_bug`）走**条件边**自动分流。

---

## 5. 建议的链路骨架

```
Alertmanager → [适配层，不是 agent] → POST /tickets(含 cmdb_ci + 时间窗)
                                    → POST /tickets/{tid}/run
   ↓  workflow：problem-log-diagnose 的 metric-first 变体
triage(告警 + 工单) → scope(service-scoper，定位服务)
                       ├→ metrics   ★改造后：定 step、看形态、拉基线、报 pod 分布
                       └→ infra     （join: all，required_edges 含 scope）
                     → locate(服务 → 仓库)
                     → rca        ★收结构化 metrics
                     → plan(remediation-planning-analyst)
                     → diagnose-output(人工门)   ← 人裁定：代码问题还是容量问题
                     → create-ticket(next_workflow="problem-diagnose-fix")  ← 机制已现成
```

### 5.1 窗口怎么给（容易做错）

- 告警说"持续 1–2 分钟"，但诊断窗口应该**大于**告警窗口 —— 需要**前后文**
  （基线 + 之后是否恢复）。**别把 `window_start = 告警起` 直接传进去。**
- 窗口跨度受 `datasource_max_range_hours` 上限约束；
- `step_seconds` 要**显式下发**而不是让模型选（见 §3.4 与 §2.2 fact 3）。

### 5.2 告警本身的信息别丢

告警 labels（pod 名 / namespace / 实例）比 `service` 名**精确得多**。
数据工具只接受 `service`（+ infra 侧可传 pod）。
**若告警指向单个 pod，"单副本 vs 全副本"这个分叉在入口处就能定位**，不用等 agent 猜。

---

## 6. 明确**不建议**做的

| 不建议 | 理由 |
|---|---|
| **加 `change-analyst`**（变更关联） | CMDB 本体已声明 `change` / `incident` 节点，但**当前数据为 0**（§2.4）——能力是空的。本仓有"加了东西但消费方为零 / 看着全绿实则为空"的教训。**先把变更数据喂进 overlay**，再决定它是 `rca` 的一路输入还是独立节点。 |
| **加第二个指标类 agent** | 工具绑定是 per-agent 的 DB 行，新 agent = 新行 + 新绑定 + seed；而**绑定缺失是静默的**（`mcp_server_ids` 为 NULL ⇒ 零工具 ⇒ run 照样绿）。新增 agent 与 `metrics-analyst` 工具集完全相同，只会多一个漏配点。 |
| **让平台自己判"持续 1–2 分钟"** | 见 §7.3。 |

---

## 7. 待决策

### 7.1 你要的形态是哪个？

| 选项 | 形态 | 改动面 |
|---|---|---|
| **A. 被动接警** | Alertmanager 触发 → 适配层建单 → 现有诊断链跑 | 小：一个适配层 + 改造 `metrics-analyst` + 一条新 workflow。阈值判据留在 Prometheus |
| **B. 平台自己巡检** | 周期性拉指标、自己判持续性、自己建 run | **另一个产品形态**：现在唯一的周期循环是审批 Sweeper，它不建 run。配额 / 成本 / 告警噪声都要另设计 |
| **C. 手动跑** | 人看到告警，去页面发起一次诊断 | 最小：连适配层都不用，推一条新 workflow 即可 |

### 7.2 §4.3 的路由分叉：选 (a) 人工裁定还是 (b) 条件边自动分流？

### 7.3 "持续 1–2 分钟"该由谁判？（倾向：监控系统）

倾向**留在监控系统**，agent 只负责解释与追问。理由：

- agent 判"持续"要花一轮 LLM + 一次查询，**慢且不确定**；
- **平台没有常驻形态** —— 唯一的周期循环是审批 Sweeper，它不建 run；
- 阈值是运维策略，改阈值不该需要改 prompt。

若选 B（平台自巡检），则技术上 `POST /run` 就能做（cron 建 run），
但**配额 / 成本 / 噪声**要另设计。

---

## 8. 本文事实的核实方式

每条结论都是**读源码 / grep** 得到的，不是推测。核实产物：

```bash
# 编队与 agent 定义
agentflow/agents/registry.py:25-45          # 9 诊断 + 7 修复
agentflow/agents/schemas.py:177-189         # MetricsEvidenceSchema（5 标量）
agentflow/agents/schemas.py:191-202         # InfraEvidenceSchema
agentflow/agents/prompts.py:214-231         # metrics-analyst prompt（:227 禁止追问）

# 指标契约（隔壁仓）
aiops-mcp-servers/servers/aiops-datasource-mcp-server/
  src/.../tools/datasource.py:232-256       # 签名 + step_seconds 默认 30
  src/.../backends/prometheus.py:29,43-47   # cpu_percent 表达式（求和后相除）
  src/.../backends/prometheus.py:81-83      # available_metrics() 只有 5 个
  src/.../backends/prometheus.py:91         # value = max（峰值）
  src/.../backends/prometheus.py:115        # rate 窗口 max(step*2, 60)s 地板
  src/.../backends/prometheus.py:165-178    # 返回 series（≤120 点）+ series_count
  src/.../backends/k8s.py:105-124           # check_infra 逐 pod 返回

# 链路
scripts/problem-log-diagnose.workflow.yaml:110,158-168,211
agentflow/seed/workflows/scenario2-bug-fix.yaml:178-193,202

# 入口（无告警摄入）
agentflow/api/app.py:454,833,963            # 仅有的三个入口
agentflow/approval/sweeper.py               # 唯一周期循环，不建 run

# 变更数据为空
aiops-mcp-servers/.../data/cmdb-entities.json   # change=0, incident=0
aiops-mcp-servers/.../backends/entity_graph.py:89  # event 落点（incident/change）
```

**未核实 / 未验证的部分**（如实标注）：

- 本文**没有跑过任何真实 run**，全部是静态阅读结论；
- `series_count` 在实际 Prometheus 上返回几条、`step_seconds` 取多少能看清
  1–2 分钟平台期，需要**在 testbed 上实测**（本机 44772 沙箱 / 8300 MCP datasource）；
- §2.4 的 incidents overlay 机制在**配了 `datasource_incidents_path` 的环境**下是否
  真有数据，未确认。

---

## 9. 下一步

等 §7 拍板后，本文可以落成一份带优先级的实施清单
（区分哪些在 `multi-agent-workflow`、哪些在 `aiops-mcp-servers`），
或直接产出那条 metric-first 的诊断 workflow YAML。
