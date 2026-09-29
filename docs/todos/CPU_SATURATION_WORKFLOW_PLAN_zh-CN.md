# CPU 居高不下：metric-first 诊断链改造方案

> 状态：**方案稿 / 待评审**（2026-09-29）。**未动任何代码**，本文只有结论与清单。
> 读者：**你**，在决定"CPU 这块怎么落地"的时刻读它。§2 是实测（含两处推翻上游判断）、
> §5 是两张流程图、§6–§8 是三批改动、§10 是验证步骤。
>
> 前身：`CPU_SATURATION_DIAGNOSE_zh-CN.md`（2026-09-24 调研稿）—— 2026-09-29 **并入本文并删除**。
> 它当时的三处结论已被实测推翻，更正保留在 §3；它独有的三条能力边界折进了 §1.2 与 §12。
>
> 触发场景：监控发现某服务 **CPU 持续 1–2 分钟居高不下**，希望平台给出根因分析与修复计划。

---

## 0. 结论摘要（tl;dr）

1. **不需要新增节点、也不需要新增 agent。** DAG 的 5 种 kind 与现有诊断 agent 足够表达这条链；
   本流程要用的四个 agent 在三个租户里**都已绑好** `aiops-datasource`，数据面一行不用改。
2. **取证接口不够，但缺的不是数据源，是一处查询范式 + 三处字段**：per-pod 维度被 `sum()` 折叠了、
   限流指标没暴露、limit 只能从 `describe_pod` 的自由文本里读（而算出来的还是错的）。
   三样在 Prometheus 里**都有数据**（§2 实测）。
3. **入口那半截 APM 仓已经做完了**：检测器（`cpu_usage ≥ 0.9`）+ L3 持续性 + 「Analyze → `POST /run`（带时间窗）」
   都在，缺的只有**数据源那一条配置**。agentflow 侧确实没有告警摄入，但这条链不经过它。
4. **"持续 1–2 分钟"由监控侧判，agent 只负责解释与追问** —— 与前身调研稿当时的结论一致，
   而且**已经实现了**（L3 累计轮数），不需要平台自巡检。
5. **诊断完的分叉走人工裁定门**，不做按 `root_cause_type` 的条件边自动分流 ——
   因为容量类根因**今天没有可执行的下游**（§9，`remediate` 只产计划不执行）。
6. 三批改动可独立开工：agentflow（新流程 + 证据形状）/ MCP（per-pod、限流、配额）/ APM+UI（自动开单 + 展示）。

---

## 1. 先回答两个问题

### 1.1 既有节点够不够？——够，一个都不用加

| 需要的能力 | 现成的载体 | 位置 |
|---|---|---|
| 多路并行取证 | `join: all` + `required_edges` + 边上的 `when` | `core/dag.py:106,385-405` |
| 人工裁定点 | `kind: approval`（`on_reject: continue` = 驳回不中止 run） | `core/dag.py:215-222` |
| 证据不足时停住 | `kind: halt`（一旦执行其余 PENDING 全 SKIPPED） | `docs/constraints/03` §3.1 |
| 升级 → 建单 → 钉下游流程 | `kind: ticket` + `params.next_workflow`（字面量） | `scripts/problem-log-diagnose.workflow.yaml:186-211` |
| 五个诊断 agent | `triage` / `metrics-analyst` / `infra-locator` / `code-locator` / `root-cause` | `agentflow/agents/registry.py:25-49` |

**为什么不新增 agent**（前身调研稿也反对）：agent 的工具来自**绑定**，而绑定不在代码里——
少一行 `mcp_server_ids` 就是零工具，且**中间没有任何一步会报"绑定缺失"**，症状只是
「agent 未输出合法 JSON」（`docs/constraints/06` §6.0 有实测）。新增一个与 `metrics-analyst`
工具集完全相同的 agent，只是多一个这样的漏配点。

### 1.2 MCP 取证接口够不够？——不够，但缺的都是"暴露"而不是"采集"

`query_metrics` 的契约（`aiops-mcp-servers/.../backends/prometheus.py`）：

- 可用指标**只有 5 个**：`["cpu_percent","memory_percent","disk_percent","error_rate","p95_latency_ms"]`（`:81-83`）；
- `value` 按设计取**峰值**（`:86-96` 的 `_aggregate`，注释写着"诊断关心是否打满而非均值"）；
- `rate()` 窗口 = `max(step_seconds*2, 60)s`，**60 秒地板**（`:116`）；
- 返回 `series`（≤120 点）、`series_count`、`window` 回显（`:165-179`）。

还有**两块取数是空的**，它们决定了这条链今天解释不了什么：

- **变更 / 事件关联**：CMDB 本体已声明 `incident` / `change` 节点，overlay 机制也在
  （MCP 侧 `backends/entity_graph.py` 的 `load_incidents_overlay()`，路径由 `DATASOURCE_INCIDENTS_PATH` 配，
  **留空是合法状态**）。但主数据文件 `data/cmdb-entities.json` 里 **`change` = 0、`incident` = 0**；
  另有一份 `data/cmdb-incidents-otr.json`（1 条 incident + 1 条 `incident_cluster_app` 边）**不自动加载**
  （本机 `.env` 没设那个变量）。
  ⇒ **"CPU 突增是不是昨天那个发布引起的"今天问不出来** —— 而这是 CPU 场景最想拿到的旁证。
- **K8s 的"配置面"**：`check_infra` 只回 pod 列表（`status` / `restarts` / `reason`），
  `describe_pod` 是 8000 字自由文本 —— **没有 replicas / HPA / events**。
  ⇒ "副本够不够、有没有在自动扩"只能从 pod 数量间接数出来（要不要补见 §12）。

> 另记一条**将来接告警时**的判据：数据工具只接受 `service`（infra 侧可传 pod），
> 而告警 labels 里的 pod / namespace 比 service 名**精确得多**。告警若指向**单个 pod**，
> "单副本跑飞 vs 全副本都跑飞"这个分叉在**入口处**就能定位，不必等 agent 猜（呼应 §7.2 的 `by_pod`）。

---

## 2. 本次实测（上游调研稿没有，其中两条推翻它的判断）

拿 MCP 的原式 PromQL 直接打本机 testbed Prometheus（`localhost:19090`，order-service，单副本、`limit: "1"`）：

| 查询 | 结果 |
|---|---|
| MCP 的 `cpu_percent` 分子 / 分母 / 整式 | **各 1 条序列，且无 `pod` 标签** |
| 去掉 `sum()` 的 `rate(container_cpu_usage_seconds_total{...}[60s])` | 3 条，**带 `pod`** |
| 去掉 `sum()` 的 `container_spec_cpu_quota{...}` | 2 条，**带 `pod`** |
| `container_cpu_cfs_throttled_periods_total` | **存在**（9 条序列） |
| `cpu_percent` 同刻对照：现行过滤 vs 加 `container!=""` | `0.255677` vs `0.255562`；**分母 `2.0` vs `1.0`** |

三条结论：

1. ⚠️ **`series_count` 恒为 1，它证明不了"几个 pod 在贡献"** —— `sum()` 已经把标签全丢了。
   前身调研稿把它当成 pod 计数是**错的**，不要把它写进 schema 当证据。
2. ⚠️ **`container!="POD"` 拦不住 cAdvisor 的 pod 级聚合序列**（它的 `container` 标签是**空串**，
   不是 `"POD"`），于是分子分母各自被算了约两遍：**一个 `limit=1` 核的容器，工具算出来的配额是 2 核**。
   比值因为两边同倍放大而几乎不变（同刻对照只差 0.05%），**错的是"限值"这个数本身**。
3. **per-pod 与限流的数据都在 Prometheus 里**，是查询范式把它们挡在门外了。

---

## 3. 对上游调研稿的更正

| 调研稿说 | 实情 | 影响 |
|---|---|---|
| §2.2 "`series_count` 有这个信息（返回了几条序列）" | 恒为 1（§2） | 别把它当 pod 计数的证据 |
| §2.1 "触发入口：**没有**" | agentflow 侧确实没有，但 **APM 仓已经做完**：检测器 + L3（`persistence_rounds`）+ `_analysis_window`（`router/problems.py:549`，±10min、≥30min、≤24h）+ Analyze → `POST /run` | 入口只剩**一条配置**要补 |
| §1 "16-agent 编队（9 诊断 + 7 修复）" | 现在是 **9 + 11 = 20**（`deployer` / `smoke-tester` 等已加） | 只是记数陈旧 |

另外两条**调研稿没提、但决定方案形状**的：

- **Problem Center 是按节点 id 硬取值的**：`rca` / `plan` / `diagnose-output`（`aiops_apm/diagnosis/from_agentflow.py:43-45`），
  证据链只读 `logs` / `locate`（`:413`），节点中文名表也只覆盖那几个（`:424`）。
  ⇒ 新流程**必须沿用这些节点 id**，否则页面空白；要让指标证据出现，得在 APM 侧加两行。见 §8.2。
- **testbed 里那条 `PodCPUHigh` 告警规则是空转的**：`manifests/prometheus/prometheus.yml` 没有 `alerting:` 段，
  集群里也没有 Alertmanager。别指望它。

---

## 4. 已定的事项（决策记录）

| # | 决策 | 理由（一句话） |
|---|---|---|
| 1 | 范围 = agentflow + MCP(per-pod) + APM(自动开单 + 展示) | 三块缺一，端到端就不成立 |
| 2 | 触发 = **人在 Problem Center 点 Analyze** | 不写告警适配层，也不新增常驻巡检 |
| 3 | 分流 = **人工裁定门**（沿用 `diagnose-output`） | 容量类根因今天没有可执行下游（§9） |
| 4 | 证据改造落在**共享的** `metrics-analyst`（prompt + schema） | 实测三个租户内置 agent 的 `system_prompt` 都是 `NULL` ⇒ 走代码回退，**重启即生效，不用推库** |
| 5 | 验证 = 注入真 CPU 故障跑完整闭环 | 只压 CPU，不写磁盘（磁盘满会引入第二个症状） |

---

## 5. 执行流程总览（两张图）

第一张是**这东西怎么跑起来**（跨三仓的执行流程，标 ★ 的是本次新增/改动的那两处）；
第二张是**run 起来之后图里怎么走**（workflow 的 DAG）。

### 5.1 端到端：一次 CPU 告警的完整旅程

```
[集群] order-service CPU 打满（limit 1 核，任何忙循环都能打满）
   │  cAdvisor 暴露 container_cpu_usage_seconds_total；
   │  testbed Prometheus(:19090) 每 5s 抓一次
   ▼
[APM :7070] 按 interval=60s 拉 /api/v1/query  ◀── ★ 本次要新增的就是这一条 monitor_target
   │  采集 → L0 抑制 → L1 检测(cpu_usage ≥ 0.9) → L2 关联(metric↔log) → L3 持续性(rounds=1)
   ▼
problem_record(state=pending, detection_type=metric)      ← 约 1 分钟出单
   │  页面 5s 轮询
   ▼
[Problem Center] 人看到这条记录 → 点 Analyze
   │  POST /v1/problems/{id}/analyze   ◀── ★ 改成不传 workflow_id，由 APM 按类型选流程
   ▼
[APM] _analysis_window：first_seen−10min → last_seen+10min（夹到 now、最短 30min、最长 24h）
   │  inputs = { bug_report, window_start, window_end, review_feedback }
   │  POST agentflow /run   （顺带把租户从 APM 的 default 桥到 agentflow 的 otr）
   ▼
[agentflow :8000] POST /run → 建 run 行 → DAGExecutor 开跑（见 5.2）
   │
   ├─ 门「通过」→ kind:ticket 建单 → APM 回读工单号 → 问题单转 escalated
   └─ 门「驳回」→ run 仍判 done，留痕在 APM 的 diagnose_decision（不中止、不报错）
         │
         └─ （下游，本轮不改）问题单 escalate 的工单会跑 problem-diagnose-fix：
             plan → … → deploy → verify-deploy → ticket-done → 回传 APM /ticket-status
```

★ 标的两处是本次**新增/改动**的；其余（检测器、L3 持续性、窗口推导、Analyze 桥、建单回读）都是现成的。

### 5.2 workflow 的 DAG（10 个节点）

```
 inputs：bug_report / window_start / window_end / review_feedback
   │
   ▼
┌──────────────────────────────────────┐
│ triage   症状分类（无工具，只读工单）  │
└──────────────────┬───────────────────┘
                   │ 4 条无条件边
     ┌─────────────┼──────────────┬──────────────┐
     ▼             ▼              ▼              ▼
┌─────────┐  ┌─────────┐   ┌─────────┐   ┌──────────┐
│ metrics │  │  infra  │   │  logs   │   │  locate  │
│ ★主证据 │  │ 当前态   │   │  旁证    │   │ 服务→仓库 │
│ abort   │  │continue │   │continue │   │ continue │
└────┬────┘  └────┬────┘   └────┬────┘   └────┬─────┘
     │            │             │             │
     └────────────┴──────┬──────┴─────────────┘
                         │ join: all（等齐四路）
                         ▼
                  ┌──────────────┐
                  │     rca      │  root-cause：收整份 output（不是 .summary）
                  └───┬──────┬───┘
     insufficient==true│      │
              ┌────────┘      └────────┐
              ▼                        ▼
         ┌────────┐              ┌──────────┐
         │  halt  │              │   plan   │  remediation-planning-analyst
         │中断终点 │◀─────────────│  修复方案 │
         └────────┘ found==false └────┬─────┘
                    （来自 locate）    │
                                       ▼
                    ┌──────────────────────────────────┐
                    │ diagnose-output                  │
                    │ kind: approval（人工裁定点）      │
                    │ on_reject: continue              │
                    └───────────────┬──────────────────┘
                       approved==true│   驳回：这个门没有出边 ⇒ 下游全 SKIPPED，
                                    ▼   run 仍判 done（留痕在门节点的 rejected 状态）
                          ┌────────────────────────┐
                          │     create-ticket      │  kind: ticket（不走 LLM）
                          │ next_workflow =        │  建单 + 钉住下游流程
                          │ "problem-diagnose-fix" │
                          └────────────────────────┘
```

**边一共 15 条**：`triage→{metrics,infra,logs,locate,rca}`、四路→`rca`、`rca→plan`、
`rca→diagnose-output`、`plan→diagnose-output`、`diagnose-output→create-ticket [when approved==true]`、
`rca→halt [insufficient==true]`、`locate→halt [found==false]`。

**运行时按"波"走**（不是逐节点串行）：

```
波1  triage                                 无入边，立即就绪
波2  metrics ‖ infra ‖ logs ‖ locate        四路并发
波3  rca                                    join: all 等齐四路
波4  plan                                   （若 rca 报 insufficient，这一波换成 halt）
波5  diagnose-output → WAITING_APPROVAL      run 挂起，等人在页面上点
波6  人 approve → resume → create-ticket
```

### 5.3 每个节点吃什么、吐什么

| 节点 | agent | 调的 MCP 工具 | 产出 | 失败姿态 |
|---|---|---|---|---|
| triage | triage | 无 | `symptom_type` / `summary` | abort |
| **metrics** | metrics-analyst | `query_metrics`（本次加 `by_pod` / `throttled_percent` / `cpu_limit_cores`） | `MetricsEvidenceSchema`（本次加 `shapes[]`） | **abort**（主证据） |
| infra | infra-locator | `check_infra` / `describe_pod` | `InfraEvidenceSchema`（本次加 `replicas`/`limits`） | continue |
| logs | log-analyst | `query_logs` / `get_trace` | `LogEvidenceSchema` | continue |
| locate | code-locator | `locate_repo` / `get_service_topology` | `CodeLocationSchema`（`found`） | continue |
| rca | root-cause | 全部（自己复核） | `RootCauseSchema`：`root_cause_type` / `confidence` / `hypotheses` | abort |
| plan | remediation-planning-analyst | 只读代码库（若绑了 git-server） | 方案：`summary` / `steps` / `options` | abort |
| diagnose-output | 人 | — | 审批记录（approver + comment） | `on_reject: continue` |
| create-ticket | 无（`kind: ticket`） | — | 租户库里一张工单 | abort |

两处**最容易踩、但图上看不出来**的语义（详见 `docs/constraints/03-dag-semantics.md`）：

- `rca` 必须 `join: all` + `required_edges` 列全 —— 默认 `any` 会让它在 triage 一完成就启动，
  四路摘要全解析成 `None`（本仓实测过，`scenario2-bug-fix.yaml:186` 有记录）；
- `halt` 一旦执行会**把其余 PENDING 全 SKIPPED**，所以"证据不足"与"跑完了"在 API 上分得开
  （`outcome: halted` vs `completed`）。

---

## 6. 批次 1：agentflow（本仓）

### 6.1 新流程 `scripts/cpu-saturation-diagnose.workflow.yaml`

形状照 `scripts/problem-log-diagnose.workflow.yaml` 抄，把 log-only 换成 metric-first：

```
triage ─┬→ metrics   ← 主证据：形状 + 基线 + pod 分布
        ├→ infra     ← 当前态：pod 状态/重启/limit
        ├→ logs      ← 旁证：伴随的错误/超时
        └→ locate    ← 服务 → 仓库
                  → rca (join: all) → plan → diagnose-output(人工门) → create-ticket
                                    ↘ halt（rca.insufficient / locate.found==false）
```

两个关键节点的入参：

```yaml
  metrics:
    agent: metrics-analyst
    require: [service, start_time, end_time]   # 缺窗口 fail-fast，别让模型编时间
    retry: 1
    params:
      bug: "$.inputs.bug_report"
      service: "$.inputs.bug_report.cmdb_ci.name"
      services: [{service: "$.inputs.bug_report.cmdb_ci.name", confidence: high,
                  evidence_source: ticket_cmdb_ci}]   # 标量是 require 的锚点，列表是 prompt 约定的形态
      start_time: "$.inputs.window_start"
      end_time: "$.inputs.window_end"
      step_seconds: 30                             # 显式下发：步长决定它看见什么曲线
    on_failure: abort                              # 主证据拿不到就不该出结论

  rca:
    agent: root-cause
    join: all                                      # 默认 any ⇒ triage 一完成就启动，params 全 None
    required_edges: [triage, metrics, infra, logs, locate]
    require: [metrics]
    params:
      metrics: "$.nodes.metrics.output"            # ★ 整个 output，不是 .summary
      infra:   "$.nodes.infra.output"
      logs:    "$.nodes.logs.output"
      code:    "$.nodes.locate.output"
      symptom: "$.nodes.triage.output.summary"
      scope_primary: "$.inputs.bug_report.cmdb_ci.name"
      trace: "本次未采集（metric-first 流程：链路数据源未接入）"   # 未采集维度显式写负证据
      start_time: "$.inputs.window_start"
      end_time: "$.inputs.window_end"
      review_feedback: "$.inputs.review_feedback"
    on_failure: abort
```

其余要点：

- `infra` / `logs` / `locate` 一律 `on_failure: continue`（旁证失败产负证据，形状同构）；
- `create-ticket` 的 `next_workflow: "problem-diagnose-fix"` —— **字面量、写在 `params` 里**（顶层会被静默忽略）；
- `halt` 两条 `when` 边（`rca.insufficient == true`、`locate.found == false`），且 **halt 不写 params**
  （reason 由触发它的那条入边搬运）；
- 复审线索 `review_feedback` 进 `rca` 与 `locate`，但**不能进 `require`**（空串会被判"不可用"，首次诊断直接失败）。

### 6.2 不进 seed manifest（刻意）

放 `scripts/` 而不是 `agentflow/seed/workflows/`。理由：`make sync-workflows` 推 seed 时
**未命中按 name 就 POST**，而 POST 会拿到新的 `created_at` ⇒ 这条流程会变成该租户**最新的一条** ⇒
抢走 `POST /tickets/{tid}/run` 不带 `workflow_id` 时的默认位（现在那个默认位是 `problem-diagnose-fix`，
`docs/design-v5.8.md` §4.15 有刻意安排）。seed 目录是"新租户默认"的载体，这条不是。

新增 `scripts/seed_cpu_saturation_diagnose.py`：**只推 workflow**（数据面不用动），
幂等按 name PUT/POST，复用 `scripts/seed_problem_log_diagnose.py` 的 `Api` 类，不复制 HTTP 胶水。

### 6.3 证据形状：`schemas.py` + `prompts.py` + `runner.py`

- `MetricsEvidenceSchema`（`agentflow/agents/schemas.py:177-189`）加一维**形状**，5 个标量与 `required` 不动：

  ```python
  "shapes": [{"metric", "pattern",          # spike | plateau | rising | falling | normal
              "sustained_minutes", "peak", "avg", "baseline_avg",
              "pods_affected", "cpu_limit_cores", "throttled"}]
  ```

- `InfraEvidenceSchema`（`:191-202`）加 `replicas` / `limits`（现在只有一个自由形态的 `resource_usage`）。
- `prompts.py`：
  - 新增 `_STEP_RULE`（与既有 `_WINDOW_RULE` 同族）：入参给了 `step_seconds` 就原样转发，没给传默认 30。
  - 改 `metrics-analyst`：把「五个指标各调用一次即够；不要反复试不同 service/uri」换成**有预算的调查**——
    ① 先看形状再看数值（`value` 是峰值，`avg`/`series` 才是形状）；② 拉一条**基线**（同窗口整体前移 24h）；
    ③ 伴随信号 `error_rate` / `p95`（"用户是否真受影响"的判据）；④ `value=null` 是**候选根因**（未设 limit）不是 0；
    ⑤ 收口纪律改成**数字预算**（总调用 ≤8 次）。
  - `root-cause`：判定优先级里已有"CPU 打满 → `infra_issue`"，补一句让它优先读 `metrics.shapes` 而不是 summary 文本。
- `runner.py`：`_MAX_ITERS["metrics-analyst"]` 从默认 10 提到 14。
  理由同 `trace-analyst`(12) / `remediation-planning-analyst`(20)：多路查询 + 形状推理需要余量，
  **撞顶的代价是整节点无输出**（`AgentOutputError` → `on_failure: abort` → 整条 run 失败）。

### 6.4 测试 `tests/test_cpu_saturation_diagnose_workflow.py`

照 `tests/test_problem_log_diagnose_workflow.py` 两段式：

- **结构守卫**：节点集；`metrics` 是唯一带时间窗 `require` 的取数节点；`rca` 的 `join: all` + `required_edges` 完整；
  `rca` 拿到的是**整个 output**（不是 `.summary`）；门节点两条入边 + 恰好一条出边 + 无驳回边 + `on_reject: continue`；
  `next_workflow` 指向**真实存在**的流程；两条 halt 触发边都在。
- **行为**：scripted runner 跑通收敛；`metrics` 失败 ⇒ run failed；`rca.insufficient` ⇒ halted 且 plan/门 SKIPPED；
  缺 `window_start` ⇒ 在 `metrics` 处 fail-fast。

---

## 7. 批次 2：MCP 取证接口（隔壁仓）

`aiops-mcp-servers/servers/aiops-datasource-mcp-server`，改动集中在 `backends/prometheus.py`。

### 7.1 修选择器（顺带修掉 §2 那条既有缺陷）

```python
def _sel(pod_re: str) -> str:
    # container!="POD" 拦不住 pod 级聚合序列：它的 container 标签是**空串**。
    # 实测 order-service：分母被算两遍，1 核的 limit 求和成 2 核。
    return f'pod=~"{pod_re}",container!="POD",container!=""'
```

`_sel` 只喂 `cpu_percent` 与 `memory_percent`（另三个指标走 `service=~` 的应用级选择器），所以波及面就这两个。

### 7.2 `cpu_percent` 的响应加四个字段（**不加参数、不加指标名**）

```jsonc
{
  // ↓ 既有字段语义与量纲逐字不动（scenario1/2、APM 检测器都依赖它）
  "metric":"cpu_percent","value":99.4,"min":…,"max":…,"avg":…,"last":…,"series":[…],"series_count":1,
  // ↓ 仅 cpu_percent 追加
  "cpu_limit_cores": 1.0,        // 服务级 = 各 pod 之和；修完选择器后才是真值
  "throttled_percent": 47.2,     // 0-100，该服务最严重的 pod（"用户能感觉到的卡"的直接判据）
  "by_pod": [{"pod":"…-xn94z","cpu_percent":99.4,"limit_cores":1.0,"throttled_percent":47.2}],
  "pod_summary": {"pod_count":3,"returned":3,"truncated":false,
                  "max_cpu_percent":99.4,"mean_cpu_percent":33.9}
}
```

- **为什么不加参数**：opt-in 的 `breakdown="pod"` 会多一个**静默**失败面——YAML 或 prompt 忘了传，
  per-pod 证据就无声消失。本仓的既有姿态正相反（未知 metric 必须报错，不得静默兜底）。
- **为什么不是新指标名**：`available_metrics()` 是错误信息与 README 表格的契约；
  加第 6 个名字只会诱使 agent 多烧一轮，而这些数**需要和 CPU 那个数并排看**。
- **`pod_summary.max` vs `mean` 就是"单副本跑飞 vs 全副本都跑飞"的判据** —— 给数，不给编造的结论与阈值。
- ⚠️ **`sum by (pod)` 必须分子分母都加**：实测分子 `by(pod)` 而分母不带时标签集不匹配，
  PromQL **直接返回空结果**（静默无数据）。
- `by_pod` 上限 20 条（按 CPU 降序），截断必须显式可见（`truncated: true`）；限流无数据时给 `null`，**不给 0**。
- 代价：每次 `cpu_percent` 多 3 个上游请求（并发、只这一个指标）。若延迟不可接受，
  退路是 opt-in 参数——但那时**必须**在 YAML 里传并写一条测试钉住，否则又是静默。

### 7.3 测试 `tests/test_prometheus_backend.py`

`container!=""` 进了每个选择器 / 分子分母都 `sum by (pod)` / 三个附加查询各自成式 /
`available_metrics()` 仍是 5 个 / **不带 per-pod 证据时响应键集与今天逐字相同**（既有消费方回归）/
3 pod 时的 `by_pod` 顺序与 `pod_summary` / 25 pod 时截断到 20 且 `truncated=true` /
限流缺失给 `null` / 非 cpu 指标**没有**这些字段。

---

## 8. 批次 3：APM 自动开单 + 页面展示

### 8.1 让 CPU 持续高自动变成问题单

> ⚠️ **本节的端点配置草图已被 `METRIC_PIPELINE_BEST_PRACTICE_zh-CN.md` §4.2/§4.3 取代**：
> 判定改为**把"持续"编码进 PromQL**（`min_over_time` + `sum by (pod)` + `>= bool`），
> 不再依赖 L3 数轮数，也顺带绕开"重叠窗口凑轮数 / 空结果静默 / 限值翻倍"三个坑。
> 下面这些结论仍然成立：检测器与 `persistence_rounds` 不用改、`app-resources` 之类的空转 job 要清。

**只需要加一条监控端点配置**——检测器与持续性阈值都已经在位：

- `config/domains.yaml` 的 `application` 域**已经有** `{signal: cpu_usage, plugin: static_threshold, params: {threshold: 0.9}, severity: high}`；
- ⚠️ 但**活库的值是 `persistence_rounds: 1`**，不是 YAML 里的 2 —— `V11__seed_live_data.sql:45` 播的就是 1，
  该文件头部第 30 行还专门写了这件事。DB 优先（`config/loader.py`），所以**以活库为准**：
  `interval_sec=60` + `persistence_rounds=1` ⇒ **约一分钟**出单。
  要严格"两分钟"有两种代价更大的做法（改 V11 那行会**连带三条日志端点**一起变；另立 domain 会
  **切断 metric↔log 的 L2 关联**），建议先接受 1。
- 采集链路是现成的：`collectors/__init__.py:40` 已分派 `metric + prometheus → http_metrics`，
  而它的 `rows_path` **默认就是 `data.result`**（`http_metrics.py:64`），本来就是照 Prometheus 响应写的。

配置形状（本机 Prometheus 是 port-forward 的 `19090`）：

```jsonc
{
  "url": "http://localhost:19090/api/v1/query",     // 容器里换成集群内地址
  "params": {"query": "<CPU 比例表达式>"},
  "rows_path": "data.result",
  "field_mapping": {"metric": "metric.metric",      # ← 由表达式 label_replace 打上的静态标签
                    "service": "metric.service",
                    "value": "value[1]", "timestamp": "value[0]"},
}
```

四条**必须**照做的理由：

1. **只能用 instant vector（`/api/v1/query`），不能改 `query_range`。**
   `FieldMapper.map_metric` 是**一行 → 一信号**（`_field_mapping.py:103`）；matrix 行里整条曲线都在
   `values[]`，只能映射到 `values[0]` ⇒ 水位线永远钉在最旧那点、再也推不动。
2. **表达式必须 `sum by (pod)`（分子分母都加）+ `label_replace` 打静态标签。**
   `sum()` 会把标签（含 `__name__`）全丢掉，映射取不到值就**静默落到默认的 `"unknown"`**，
   而检测器按 `signal: cpu_usage` 匹配 —— 于是**永远不命中**。
3. **量纲是 0–1 比例，表达式末尾 `/100`。** 域约定是比例（`threshold: 0.9`），而 MCP 工具返回 0–100 的百分数。
   搞错就是"0.9% 的 limit 就触发"的常真检测器，看着配了、实际不响。
4. 水位线下推多带的 `start`/`end` **Prometheus 直接忽略**（实测 200 且结果完整），
   **APM 侧零代码改动**；验证用 `POST /v1/monitors/{id}/test`（跑一轮、不动水位线，能立刻看出
   `metric` 有没有映射成 `cpu_usage`）。

落点：迁移 `V13__seed_testbed_metric_target.sql`（照 V9 的形状，含 GUC 注入与 `ON CONFLICT DO NOTHING`）
+ 可重复执行的 `docker/seed_testbed_metrics.py`（V9 头部就写着"迁移是一次性的，之后改配置走脚本"）
+ `make seed-testbed` 扩展。⚠️ 别拿 `docker/seed.py` 当模板：它是**过期的**
（写的是 `source_type: http_metrics` + `metric_path`，`collector_for` 早就不认了）。

### 8.2 让指标证据在页面上看得见

`src/aiops_apm/diagnosis/from_agentflow.py`：

- `_EVIDENCE_NODES`（`:413`）加 `("metrics","指标证据")`（**排第一**，metric-first 流程里它是主证据）与 `("infra","基础设施状态")`；
- `_NODE_LABELS`（`:424`）补 `metrics` / `infra` 的中文名（其余节点 id 我们沿用了既有流程，标签已在）；
- 若希望数值细节也进页面，再加一张 `_EVIDENCE_SUPPORTING_KEYS` 映射（新节点读一个 `evidence_text` 字段，
  既有两条保持空串 ⇒ **行为逐字不变**）。

### 8.3 让 CPU 那条流程真的能被点到

**放在 APM 服务端**，不是前端。理由：`_detection_type(rec)`（`router/problems.py:72`）已经能判
`log`/`metric`/`combined`，`_resolve_workflow_id(request, name)`（`:1328`）已经能"名字 → 最新 id"；
而且 UI 自己那段注释写着**不要**用 `detection_type` 做等值判断。

- `settings.py` 加 `metric_workflow_name` / `log_workflow_name`（环境变量可覆盖）；
- `analyze` 的 `workflow_id` 变成**可选**：空了就按 `_detection_type` 选流程名（`metric` → 新流程，
  `combined`/`log` → 既有日志流程）；老客户端显式传 id 的行为**不变**；
- UI 侧因此可以**净删**那 25 行按名字硬解析的代码。找不到流程时仍然亮红（`_resolve_workflow_id` 抛 404 并点名）。

---

## 9. 下游契约与一处"没有下游"的真相

- **节点 id 是契约**：`rca`（读 `hypotheses[0]` / `root_cause_type` / `confidence`）、
  `plan`（读 `summary` / `steps` / `options`）、`diagnose-output`（审批门）。
  改名 = 页面空白，且没有任何报错。
- ⚠️ **容量类根因今天没有可执行的下游**：`docs/TODO.md` §22 —— `remediate`（infra-remediator）
  **只产计划、不执行**（ActionExecutor 全仓没接线，实测那次它可用工具是 `[]`、调用 0 次）。
  也就是说"CPU 高是因为副本不够"这条结论走到修复段时，scale/patch **不会发生**
  （代码路是真执行的）。**这正是本轮选"人工裁定门"而不是"条件边自动分流"的理由**：
  自动分流到 infra 分支，下游也是空转。

---

## 10. 端到端验证（注入真故障）

按仓库硬性要求，**验证要交产物**（命令 + 退出码 + 关键输出行），不写"已验证"。

1. 重启 agentflow API 与 worker（prompt/schema 只在代码里，改完必须重启，否则静默跑旧 prompt）。
2. 重启 MCP datasource（8300，**必须在它自己目录启动**）。
3. `scripts/seed_cpu_saturation_diagnose.py --tenant otr --dry-run` → 去掉 `--dry-run` 再跑，回读校验通过。
4. APM：`make migrate`（V13）→ `POST /v1/monitors/MT-0004/test` 先证明映射对（`metric` 不能是 `unknown`）。
5. **只注入 CPU**（`fault-inject/scenario1.sh` 的第②段忙循环，**不写磁盘**——磁盘满会引入第二个症状）。
6. 确认 Prometheus 看得见：`cpu_percent` 应从基线 ~0.3% 升到 ~100%
   （今天实测空闲态 min/max/avg = `0.162/0.552/0.304`；order-service `spec.replicas: 1`、`limit: "1"`）。
7. 等 1–2 个采集轮 → problem_record 出现在 Problem Center（`detection_type: metric`）。
8. 点 Analyze → 断言 run 的 `rca.output`：`root_cause_type == infra_issue`、`hypotheses[0]` 指名 CPU、
   `metrics.shapes[0].pattern == "plateau"`、`baseline_avg` 显著低于 `peak`。
9. 页面断言：证据链出现「指标证据」「基础设施状态」；任务列表是中文节点名（不再是裸 node id）。
10. 收尾：杀掉忙循环（`scenario1-recover.sh`）；连续跑场景前清 ES 日志窗口
    （`curl -X DELETE :19200/app-logs`，见 `docs/constraints/10`）。

⚠️ **per-pod 那一项在本机验不出差别**：order-service 是 **1 副本**。要验"单副本跑飞 vs 全副本都跑飞"，
得先扩到 2–3 副本再只压其中一个 pod —— 这一步单独做，别混进主验证。

---

## 11. 风险与未决

| 风险 | 说明与缓解 |
|---|---|
| **模型的时间算术** | 基线窗口 = 主窗口整体前移 24h，由模型自己算 ISO8601；算错/不算 ⇒ 无基线（软失败）。提示词给死规则 + 允许"算不出就标未采集"，验证时断言 `baseline_avg` 真来自另一条查询 |
| **轮次预算** | 主指标 + 基线 + 2~3 伴随信号 ≈ 6 次调用 ⇒ 默认 10 轮容易撞顶。两道防线：`_MAX_ITERS` 提到 14 + 提示词数字预算 |
| **改共享 prompt 波及 scenario1/2** | 那两个场景的指标节点也吃这份 prompt。方向一致，但要跑 `make test` 回归 + 各自一次真实 run |
| **无 limit 的容器静默不触发** | 没有 `container_spec_cpu_quota` ⇒ APM 那条监控**零行**、永远不告警（MCP 侧则如实回 `value: null` + 归因提示）。要写进 seed 脚本头部；将来可加一条"按核数"的端点，但本机没设 limit 会**双触发**，故本轮不做 |
| **L3 计的是累计轮数，不是连续轮数** | `l3_verify.py:34-60`：断轮不清零。"持续"比读起来弱；真要严格得改代码 |
| **两处 CPU 定义并存** | APM 触发用的 PromQL 与 MCP 工具里的 `cpu_percent` 是两份表达式、两种量纲。两边都要写交叉引用注释，否则"开单说 95%、诊断说 30%"会同时出现在人眼前 |
| **`by_pod` 增加上游请求** | 每次 `cpu_percent` 多 3 个并发请求；若延迟可见，退路是 opt-in 参数（但必须传 + 测试钉住） |
| **信号去重哈希不含 service** | `http_metrics.py:73` 用 `md5(metric\|value\|timestamp)`：两个 pod 同值同刻会塌成一条信号。不影响触发（上面那条 pod 自己会成行），但降低快照保真度 |

---

## 12. 明确不做

| 不做 | 理由 |
|---|---|
| **Alertmanager → 自动建 run** | 需要新适配层 + 集群部署 Alertmanager，且并发配额、重复告警、失败重试都要另设计。判据该留在监控系统，agent 只解释 |
| **新增 CPU 专用 agent** | 多一个"绑定缺行 → 零工具 → 静默失败"的漏配点，工具集与 `metrics-analyst` 完全相同 |
| **按 `root_cause_type` 条件边自动分流** | 下游 infra 分支今天不执行（§9） |
| **k8s 侧补 replicas / HPA / events 工具** | 有价值但不阻塞本链：CPU 判断靠指标 + `describe_pod` 就能立住。单开一条 |
| **加 `change-analyst`（变更关联）** | 本体已声明 `change` / `incident` 节点，但主数据文件里**两个都是 0** —— 而"加了东西但消费方为零 / 看着全绿实则为空"正是本仓踩过的形态。**先把变更数据喂进 overlay**（`cmdb-incidents-otr.json` 已有一份现成的），再决定它是 `rca` 的一路输入还是独立节点 |
| **平台自巡检（cron 建 run）** | 现有唯一周期循环是审批 Sweeper，它不建 run；"持续"由 APM 的 L3 判更省 |

---

## 13. 本文事实的核实方式

```bash
# agentflow（本仓）
agentflow/core/dag.py:106,297                 # join 默认 any / kind 白名单
agentflow/agents/schemas.py:177-189,191-202   # Metrics/InfraEvidenceSchema
agentflow/agents/prompts.py:231               # 「五个指标各调用一次即够」
agentflow/agents/runner.py:31-42              # _MAX_ITERS（metrics-analyst 走默认 10）
scripts/problem-log-diagnose.workflow.yaml:110,186-211   # rca 的指标负证据 / 建单与钉流程
tests/test_problem_log_diagnose_workflow.py   # 结构守卫的写法（本方案的测试模板）

# MCP（隔壁仓）
aiops-datasource-mcp-server/backends/prometheus.py:81-83,86-96,116,139-153,165-179
aiops-datasource-mcp-server/backends/k8s.py:94-151,154-169

# APM（隔壁仓）
src/aiops_apm/pipeline/l3_verify.py:34-60                 # 持续性判定
src/aiops_apm/migrations/V11__seed_live_data.sql:30,45    # 活库 persistence_rounds = 1
src/aiops_apm/router/problems.py:72,549-569,582,604,1328  # _detection_type / 窗口 / analyze / 名字→id
src/aiops_apm/collectors/__init__.py:40                   # metric+prometheus → http_metrics
src/aiops_apm/collectors/http_metrics.py:64               # rows_path 默认 data.result
src/aiops_apm/collectors/_field_mapping.py:24,103         # value[1] / metric.__name__ / 一行一信号
src/aiops_apm/diagnosis/from_agentflow.py:43-45,413,424   # 页面读哪些节点 id
service-intelligence-platform-ui/js/app.js:4080-4113      # Analyze 按 name 硬解析

# 本机实测（2026-09-29，只读查询，未改任何东西）
curl localhost:19090/api/v1/query   # series_count=1；per-pod 去掉 sum() 后可得；
                                    # 限流指标存在(9 条)；limit 被算成 2 核
kubectl -n order get deploy order-service   # replicas=1
```

**未核实 / 待实现时确认**：

- 活库 `domain_config` 那一行与 `V11` 快照是否逐字一致（本文按 V11 读，**上机前用管理 API 再确认一次**）；
- `label_replace` 的两种写法（空源 vs 命名源）在本机 Prometheus 上都可用，落地时取命名式；
- 本文**没有跑过任何真实 run**，也没有改动任何代码——§10 的每一步都还是待执行。
