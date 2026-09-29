# 指标采集与判定：最佳实践 + 落地清单

> 状态：**待执行**（2026-09-29）。§1 是流程图总览，§2–§3 是实测现状，§4 是推荐形态，**§6 是照着做的执行清单**。
> 读者：**执行者**（先读 §4 再照 §6 做）+ 评审者（§2/§3 是"为什么要改"的依据）。
>
> **与另一份的分工**：本文管**上游**（采集 → 判定 → 成单）；
> `CPU_SATURATION_WORKFLOW_PLAN_zh-CN.md` 管**下游**（人点 Analyze 之后那条诊断链）。
> 两者的接缝是 `problem_record`：本文负责让它**该出现时出现**，那份负责它**被点开之后跑得对**。
>
> 触发场景：监控发现某服务 CPU 持续居高不下，希望它**自动变成 Diagnosis Center 里的一条待分析问题**。

---

## 0. 结论摘要（tl;dr）

1. **判据一句话**：**谁手里有序列，谁判"是不是异常"；谁需要追问，谁答"为什么"。**
   现在两件事错位了 —— 采集层用"数轮子"近似持续性，而没序列的 agent 却常被期待判持续。
2. **最佳形态是四层分工**：Prometheus（唯一序列真源）→ **一条确定性 PromQL 判完"持续"** →
   APM 只搬运并成单 → agent 只解释。§4。
3. **关键改动只有一条查询**：把"持续 3 分钟"用 `min_over_time` + `sum by (pod)` 编码进 PromQL，
   于是**每个采集轮只拿一个标量就够了**（APM 的采集器本来也只能吃标量），
   而且顺带绕开"轮数近似 / 重叠窗口凑数 / 漏轮不重置 / 空结果静默"四个坑。§4.2。
4. **不需要 Alertmanager，也不需要新组件**：现有 APM 链路只缺**一条 metric 监控端点配置**。
   `POST /v1/alerts/run` 是"手动跑一轮"，**不是**接警入口 —— 别把它当成 Alertmanager 的替代。
5. **两条判据线，同一个窗口**：**症状线**（被限流 > 5% —— 用户已经在等）+ **风险线**（CPU ≥ 80%），
   都用 **3 分钟** 窗口，让一次故障落在**同一条记录**里。**"持续 10s / 30s"那类判据在这套链路上不成立**：
   1 分钟的 `rate()` 窗口会把"打满 10 秒"摊薄成 ~17%，门限永远够不着。§4.2.1。
6. 下午的活分三批，**改动面很小**：testbed 清 3 个空转 job + 给 Prometheus 加 PVC；
   APM 加**两条**端点（+ 一条新 detector）+ **一行**代码让 per-pod 标签传得下去；然后注入故障跑一次端到端。§6。

---

## 1. 详细流程图（五段：从 pod 里的计数器到 Diagnosis Center）

> **这一段是总览**：★ = 本次要做的改动，⚠️ = 今天已查实的缺陷。各段的细节在后面的对应章节。

### ① 数据在哪产生、怎么存（只读，改动集中在存储）

```
   K8s Pod：order-service（limit = 1 核）
     └─ 应用容器 → cAdvisor 暴露四类计数器：
          container_cpu_usage_seconds_total           「用了多少 CPU」
          container_spec_cpu_quota / _period          「limit 是多少」
          container_cpu_cfs_throttled_periods_total   「被掐了几期」
          container_cpu_cfs_periods_total             「总共几期」
                     │  kubelet 每 5s 抓一次
                     ▼
   ┌─ Prometheus :19090 ──────────────────────────────────────────┐
   │  job=kubernetes-cadvisor    ✅ 有数据（实测 56 条序列）        │
   │  job=kubernetes-kubelet     ✅ 有数据                        │
   │  job=app-metrics（/actuator/prometheus，打 service 标签） ✅   │
   │  job=app-resources          ⚠️ 三个 target 全 up=0  → ★删     │
   │  job=kube-state-metrics     ⚠️ 集群里没这个 pod，0 series ★删 │
   │  job=node-exporter          ⚠️ 同上                       ★删 │
   │  TSDB：⚠️ 今天**没有 PVC**（写在容器可写层；实测 5 天里只有  │
   │        4 段数据）→ ★加 PVC + retention 显式化               │
   └──────────────────────────────┬───────────────────────────────┘
                                  │  序列只存在这一层
                                  │  （APM 的 signal_snapshot 只写不读、
                                  │    无保留、连事件时间都没存 —— 别指望它）
```

### ② 判定：把「持续」写进查询里，APM 只搬运

```
   APM scheduler（每 1s tick）
     │  按 (租户, 域) 把到期的 target 收成**一轮**；两条 metric target，interval=60s
     ▼
   GET <prometheus>/api/v1/query?query=<下面这一条>
     │
     │  ┌─ 查询内部：一次问完「持续了吗」──────────────────────────┐
     │  │ ① sum by (pod) (rate(...[1m]))      每 pod 各自用了多少    │
     │  │    ÷ sum by (pod) (quota / period)  每 pod 各自的 limit    │
     │  │ ② [3m:30s]                          回看过去 3 分钟        │
     │  │ ③ min_over_time(...)                取**最小值**           │
     │  │      ⇒ 最小值都 ≥ 门限  ⟺  整整 3 分钟都高   ← 这就是"持续" │
     │  │ ④ >= bool 0.9                       输出 0/1（每轮恒定一行）│
     │  │ ⑤ and on(pod) count_over_time >= 4  样本不够就不判         │
     │  │ ⑥ label_replace ×2                  打上 metric / service │
     │  └──────────────────────────────────────────────────────────┘
     │  返回：**每个 pod 一行**：{metric, service, value: 0|1, pod: …}
     ▼
   FieldMapper → MetricSignal{service, metric, value=0|1, ts, labels{pod}}
     ⚠️ labels 今天恒为 {} （读的是顶层 labels 键，而 Prometheus 把标签放在
        metric 对象里）⇒ pod 信息在这一层就丢 → ★B3 一行改动修掉
     ▼
   每条 target 独立降级：
     HTTP 错 / 超时 / body 超限   → ✅ 响亮：target failed，轮次 partial
     返回空集 / rows_path 不匹配  → ⚠️ **完全静默**（记成 ok、success、零告警）
     一行坏数据（缺 ts / value）  → ⚠️ 整批丢弃
     ★ 用 `>= bool` 就是为了让"每轮恒定有行"，于是 signals_count 变成采集健康度
```

### ③ 漏斗 → 成单（这一段现成，不用改）

```
   一轮 = 该 (租户, 域) 下所有 target 的信号合在一起
     │
     ├─ L0 抑制：维护窗口 / 黑名单
     ├─ L1 检测：static_threshold 0.9  —— 对 0/1 输入 = 「等于 1 才异常」
     ├─ L2 关联：窗口内该服务的 metric 与 log 归成一组
     ├─ L3 验证：persistence_rounds=1（活库现值）→ 退化成去抖
     │            （"持续"已经在查询里判完，这里不再承担）
     └─ emit → problem_record(state=pending, detection_type=metric)
                     │  group_key = hash(租户 | 域 | 服务 | **该轮异常集合**)
                     │  ⚠️ 集合一变 = **另一张单**  ⇒ 两条线必须同窗口
                     ▼
              Diagnosis Center 列表（页面 5s 轮询）
```

**③ 那段是"多余的筛选"吗？**（这个问题问过一次，答案记在这里）

- **不是第二道筛选，而且今天它本来就没在筛**：活库 `persistence_rounds=1` = "第一次出现就放行"，
  所以 L1 的门限（对 0/1 输入）和 L3 的持续性**都已经是形式**。
- **但它还做着三件查询做不了的事**：
  - **L2 关联 —— 正是"两条线合成一张单"的实现者**。没有它，CPU 线与限流线会开出**两张单**；
    有它，两条异常在同一轮里按服务归成一组 ⇒ 一条记录两个 anomaly（还能把 metric 与 **log** 合并升 critical）。
  - **L0 抑制 —— 维护窗口 / 黑名单**。Prometheus 不知道这两个概念（要取代得靠 Alertmanager 的 silence）。
  - **L3 的另外两个职责**：误报率闸门（基于人工历史裁决的统计）、严重度校准。
- 真正在做"筛选 / 去重"的是 **emit 时那个 `group_key`**（`ON CONFLICT`）——
  "要不要新开一张单"的决策点在那里，不在 L1/L3。
- 结论：**别砍**。空转成本≈0（两次比较 + 一次 upsert），换来的是"与日志线共用一套漏斗"这个结构性收益。

### ④ 时间轴：注入 CPU 之后到底会发生什么

```
   t+0       起 2 个忙循环（limit 1 核）→ 立刻打满
   t+0~5s    cAdvisor 抓到；容器开始被 CFS 限流
   t+1min    Prometheus 里「过去 1 分钟」已全是 100%
   t+3~4min  两条查询**同时**变 1（都要求"整段 3 分钟都超标"）
   t+4min    APM 下一轮采集（60s 一次）把 0/1 交进漏斗
   t+4min    Diagnosis Center 出现**一条**记录，内含两条 anomaly：
                cpu_usage=1            ← 风险线
                cpu_throttled_percent=1 ← 症状线
   t+4min+   人点 Analyze → 进第 ⑤ 段
```

### ⑤ 诊断阶段是**另一条**取数路径（按需，不参与判定）

```
   人点 Analyze
     └─ APM /v1/problems/{id}/analyze（服务端按 detection_type 选流程）
          └─ agentflow POST /run  →  CPU 诊断链（见另一份文档）
                │  metrics-analyst / root-cause 按需调 MCP(:8300)
                │  → query_metrics(service, metric, start, end, step)
                │  → Prometheus /api/v1/query_range
                │  答的是「为什么 / 什么形态」：
                │     plateau 还是 spike ｜ 基线（前一日同时段）
                │     per-pod 分布 ｜ 限流 ｜ 伴随信号(error_rate/p95)
                ▼
           rca → plan → 人工裁定门 → 工单

   ⚠️ 这条路径**慢、要花 LLM 轮次、结论不确定** —— 所以它只答"为什么"，
      绝不参与"是不是异常"。判定在第 ② 段就做完了。
```

**一句话总结这张图**：序列只存在 Prometheus；**判定**是第 ② 段那一条查询（确定性、每 60s 一次、不看 LLM）；
**成单**是第 ③ 段的现成漏斗；**解释**才是第 ⑤ 段的 agent。

---

## 2. 现状：三条取数路径，打的是同一个 Prometheus

```
                        ┌────────────────────────────────────────┐
                        │  Prometheus :19090（testbed）           │
                        │  scrape 5s · 无 PVC · retention 用默认值 │
                        └───▲──────────────▲──────────────▲──────┘
      /api/v1/query_range   │              │ /api/v1/query│ /api/v1/query
                            │              │ (instant)    │ (instant)
  ┌─────────────────────────┴───┐   ┌──────┴─────────┐  ┌─┴────────────────────┐
  │ ① MCP aiops-datasource :8300 │   │ ② APM 采集器    │  │ ③ app_indicators      │
  │   query_metrics(...)         │   │  http_metrics   │  │  （直连，架构例外）    │
  │   5 个指标 · 服务级标量+series │   │  + L0–L3 + 开单 │  │   每 pod 一行         │
  │   ⚠ container!="POD"（翻倍）  │   │  ⚠ 一条 metric  │  │   ✅ container!=""     │
  │   ⚠ series 只回前 120 点      │   │    端点都没配    │  │   ✅ sum by (pod)      │
  └───────────▲──────────────────┘   └────────▲───────┘  └────────▲─────────────┘
              │ 按需（agent 自己调）           │ 定时 60s            │ 页面打开时（2s 缓存）
  ┌───────────┴──────────┐        ┌───────────┴────────┐  ┌────────┴──────────┐
  │ 9 个诊断 agent        │        │ problem_record     │  │ Smart Inspection  │
  │ metrics-analyst / rca │        │ → Problem Center   │  │ 遗留页面（给人眼看）│
  └──────────────────────┘        └────────────────────┘  └───────────────────┘
```

**一句话：能力最强的那条喂的是人眼，最弱的那条喂的是 agent。**

---

## 3. 缺陷清单（全部实测，按严重度）

### 3.1 判定语义被弱化成"数轮子"

- APM 每轮只从上游取**一个标量**（`collectors/http_metrics.py` 是 instant query；`FieldMapper.map_metric`
  一行一信号，matrix 根本映射不了），"持续"只能靠 `l3_verify.py:34-60` 的**累计轮数**近似：
  **断轮不清零**、只看身份不看数值（`anomaly_key` 刻意排除 value/time）。
- 于是 "2 轮" 可能只是 **1 次物理观测**：`window_sec` 模式下相邻轮窗口重叠，而去重集合只活在单次响应内
  （`http_metrics.py:69`）。水位线模式只推 `start` 时，采集器自己的注释也写着上游可能"退化为返回最新一页"（`:48-49`）。
- 活库 `persistence_rounds` = **1**（`V11__seed_live_data.sql:45`，不是 `domains.yaml` 里的 2）。

### 3.2 per-pod 在 APM 侧端到端丢失

- `map_metric` 读的是行里**顶层的 `labels` 键**（`_field_mapping.py:110`），而 Prometheus 的 instant 响应把标签
  放在 `metric` 对象里 ⇒ `labels` 恒为 `{}`。
- 采集器的去重哈希 `md5(metric|value|timestamp)`（`http_metrics.py:73`）**忽略 service 与 labels**；
  `anomaly_key` 因此对所有 pod 相同 ⇒ 多副本共享**同一个持续性计数器**（同一轮里多 pod 各自 +1，
  甚至可能一轮就凑满 `persistence_rounds`）。

### 3.3 采集断了自己不知道（"没数据"与"一切正常"分不开）

| 失败形态 | 姿态 |
|---|---|
| 4xx/5xx、超时、body 超限、非 JSON | 响亮：target `failed` + `error`、轮次 `partial`、`DEGRADED_SOURCES` +1、有审计行 |
| **`rows_path` 不匹配 / 返回空集** | **完全静默**：target 记 `ok`、轮次 `success`、`signals_count=0`、零告警（有测试钉着：`tests/test_collectors.py:235-249`） |
| **一行坏数据**（缺 `timestamp` / `value` 解析不出） | **整批丢弃**（映射循环无 per-row try/except），连坏行之前映射好的也一起丢 |
| `metric` / `service` 映射不到 | 静默落 `"unknown"` |

采集器**没有任何专属指标**（没有 signals_count、没有请求延迟、没有空轮计数），仓库里也**没有任何告警规则**。

### 3.4 存储面与"看着配了、实际空转"

- 🔴 Prometheus **没有 PVC**（`deployment` 的 `volumes` 只有 configMap），TSDB 写在容器可写层；
  实测 8 个 block 只覆盖 5 天里的 4 段，中间大段空洞 ⇒ **"对比昨天同时段"随时可能查到空**。
- 🔴 三个 job 空转：`kube-state-metrics` / `node-exporter`（集群里没有对应 pod，`count()` 实测 0 series）、
  `app-resources`（对三个业务 pod **全部 `up=0`**，它抓 `/metrics` 而应用暴露在 `/actuator/prometheus`）。
- 🔴 `PodCPUHigh` 规则在 `rule_files` 里，但 `prometheus.yml` **没有 `alerting:` 段**、集群里也没有 Alertmanager。

### 3.5 三条口径与三套实现

| | ① MCP（agent 用） | ② APM 采集器 | ③ app_indicators（人眼看） |
|---|---|---|---|
| 容器过滤 | `container!="POD"` ❌（1 核 limit 算成 **2 核**；实测同刻比值只差 0.05%，**错的是限值本身**） | 同 ① | **`container!=""`** ✅，且有**回归测试**钉着（`test_app_indicators.py:168-177`） |
| 粒度 | 服务级一个数（`sum()` 把 pod 抹掉，`series_count` 恒为 1） | 一行一信号 | **`sum by (pod)`** ✅ |
| 覆盖 | 5 个指标 | 5 个指标 | CPU/内存/网络/磁盘 IO/重启/存活 |
| 量纲 | 0–100 | 0–1 | 0–100（阈值 70/90） |

**同一个知识（`container!=""`）在 ③ 里被写在注释里、被测试钉住，却没跨到隔了一条街的 ① 和 ②。**

### 3.6 域配置：**活库与 YAML 已经漂了**

实测 `GET /v1/config/application`（2026-09-29）与 `config/domains.yaml` 对不上：

| 项 | `domains.yaml`（seed） | 活库（DB，**主源**） |
|---|---|---|
| `verify.persistence_rounds` | 2 | **1** |
| `ERROR` 的 `signature_aggregate.min_count` | 5 | **1** |

`loader.py` 是 **DB 主源 → 空表才 seed → last-known-good**，所以**改 YAML 对已有库无效**，
而且不会有任何提示。这既是"为什么 §4.3 那条新 detector 要走 `PUT /v1/config/{domain}`"，
也是本仓反复记录的那类"看着配了、实际不生效"。**改域配置一律以 `GET /v1/config/{domain}` 读到的为准。**

---

### 3.7 另外两条（影响下游诊断，记着就行）

- **`series` 的 120 点截断**：24h @step=30 实测 Prometheus 返回 **988 点，MCP 只回传前 120 点**
  （覆盖窗口开头那一小时），而 `value/max` 是按**全部 988 点**算的 ⇒ **数字来自整个窗口、曲线来自开头一小时**。
  `metrics-analyst` 若按提示词"看形状"，会被这条误导。
- **`signal_snapshot` 只写不读**、无保留/归档，且 `snapshot_ts` 存的是**写库时刻**、
  信号自己的事件时间**没入库** ⇒ **APM 侧没有可回溯的历史序列**。
  （设计文档说它"支撑 simple_compare 的真实历史基线"，而 `simple_compare` 的 baseline 是个静态参数。）
- 好消息：从真实 run 的 traces 统计，六个只读取数工具 **194 次调用 0 失败**
  （失败集中在工作区/写类工具）⇒ **取数的可靠性不是瓶颈，设计才是**。

---

## 4. 最佳实践：四层分工

### 4.1 第一层：数据源 —— Prometheus，是唯一的序列真源

- **序列只存在 Prometheus 里**。别指望 APM 存（§3.7：只写不读、无保留、无事件时间）。
- 要做的事：**加 PVC**（否则基线与回归对比永远不可靠）、**retention 显式写出来**（别用默认值）、
  **清掉空转的 job**（要么部署要么删配置 —— "看着配了"比"没配"更危险）。
- 目标：给定任意时间窗，`query_range` 都能取到连续数据。

### 4.2 第二层：判定 —— 一条确定性 PromQL，**把"持续"写进查询里**

这是整套的关键一步。既然采集器每轮只能吃一个标量，**就让它那一个标量本身就回答"持续了吗"**：

```promql
# 每个 pod 一行；值 = 过去 3 分钟内每 30s 测得的"CPU 占 limit 比"的最小值，≥0.9 记 1、否则记 0
(label_replace(label_replace(
  (min_over_time((
      sum by (pod) (rate(container_cpu_usage_seconds_total{pod=~"order-service.*",container!="",container!="POD"}[1m]))
    / sum by (pod) (container_spec_cpu_quota{pod=~"order-service.*",container!="",container!="POD"}
                    / container_spec_cpu_period{pod=~"order-service.*",container!="",container!="POD"})
   )[3m:30s]) >= bool 0.9)
  and on(pod)
  (count_over_time((
      sum by (pod) (rate(container_cpu_usage_seconds_total{pod=~"order-service.*",container!="",container!="POD"}[1m]))
    / sum by (pod) (container_spec_cpu_quota{pod=~"order-service.*",container!="",container!="POD"}
                    / container_spec_cpu_period{pod=~"order-service.*",container!="",container!="POD"})
   )[3m:30s]) >= 4),
  "metric", "cpu_usage", "pod", ".*"),
  "service", "order-service", "pod", ".*")
```

**五处写法各自解决一个问题**（前四种写法已在 testbed Prometheus 上逐条验过语法）：

| 写法 | 为什么 |
|---|---|
| `sum by (pod)` | 单副本跑飞**不会**被服务级平均掉（3 副本里 1 个打满 = 服务级 33%，永远不触发） |
| **`min_over_time(...[3m:30s])`** | **"整段窗口全程高于阈值"** —— 这就是"持续"的精确定义，**不再需要数轮子** |
| `>= bool 0.9` | 返回 0/1 而不是把行过滤掉 ⇒ **每轮每 pod 恒定一行**，于是 `signals_count` 成了采集健康度指标，也绕开 §3.3 的"空结果静默成功" |
| `and count_over_time(...) >= 4` | 样本不够就不判 —— 治住"漏轮也照样计数" |
| `container!=""` | 顺带修掉"1 核 limit 算成 2 核" |
| 两个 `label_replace` | `sum()` 会丢掉 `__name__`，映射会静默落到 `"unknown"` ⇒ 检测器永远不命中 |

**为什么不让 agent 判"持续"**：慢（每轮一次 LLM）、不确定、贵。
**为什么不用现在 L3 那套**：累计轮数不是连续性，还会被重叠窗口/重复样本凑数（§3.1）。
**为什么阈值留在查询里**：它是**运维参数**，改它不该动提示词、也不该重跑模型。

> 备选：`min_over_time` 去掉 `bool`（只在持续超标时才返回一行），值就是那个最小比值，
> 记录里能看到"0.93"这种更精确的数字。代价是失去"每轮恒定一行"的采集健康度信号。
> 推荐先用 `bool`。

### 4.2.1 窗口与阈值怎么定（10 秒？30 秒？3 分钟？）

**先分清两个旋钮**：**阈值**回答"多高算高"（容量语义），**时长**回答"高多久算问题"（恢复语义）。
混在一起调，最后分不清是门限太松还是窗口太短。

**卡住"时长"的是 `rate()` 窗口，算术很直白** —— 容器 limit = 1 核，**打满 10 秒后空闲 50 秒**：

| 你想要的判据 | 1 分钟 rate 窗口下实际看到 | 结论 |
|---|---|---|
| 持续 **10s** @ 80% | `(10×1.0 + 50×0) / 60` ≈ **17%** | 门限写 80% ⇒ **永远不触发** |
| 持续 **30s** @ 80% | ≈ **50%** | 30 秒打满只显示一半 ⇒ 门限 80% 也不触发 |
| 持续 **3min** @ 80% | ≈ **80%** | ✅ 窗口与判据自洽 |

⇒ **"持续 30 秒"与"1 分钟 rate 窗口"在数学上不相容**。想让 30s 显示成 ~100%，rate 窗口得缩到 ≤30s；
5s 采样下那只有 6 个样本，再往下（10s）只剩 **2 个样本** —— **一次 GC 暂停就能命中**。
`rate(...[10s])` 技术上写得出来，但那是在**测噪声**，不是在测问题。

**更根本的一条：CPU% 是原因，不是症状。** 开单该由"用户是否受影响"触发，"为什么高"是诊断的输出。
本环境里"受影响"有一个更直接、也更好测的信号：**限流** —— 容器被 CFS 掐住 ⇒ 请求被推迟。

⇒ **两条线并行，各管一件事**（两条查询今天都在 testbed Prometheus 上验过语法）：

| 线 | 判据 | 门限 | 窗口 | 用途 |
|---|---|---|---|---|
| **症状线** | `throttled_periods / cfs_periods` | > **5%**（严重分档 > 20%） | **3 分钟** | **该开单** —— 用户已经在等 |
| **风险线** | CPU 占 limit | ≥ **80%**（或 90%） | **3 分钟** | 预警 / 喂给诊断链当输入 |

症状线那条查询（与 §4.2 同形，只换了分子分母；**已在 testbed 上验过**）：

```promql
# 每个 pod 一行；值 = 过去 3 分钟内每 30s 的"被限流周期占比"的最小值，≥5% 记 1、否则记 0
(label_replace(label_replace(
  (min_over_time((
      100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m]))
    / sum by (pod) (rate(container_cpu_cfs_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m]))
   )[3m:30s]) >= bool 5)
  and on(pod)
  (count_over_time((
      100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m]))
    / sum by (pod) (rate(container_cpu_cfs_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m]))
   )[3m:30s]) >= 4),
  "metric", "cpu_throttled_percent", "pod", ".*"),
  "service", "order-service", "pod", ".*")
```

> 注意 `>= bool` 让**量纲问题消失**：输出恒为 0/1，所以 detector 的 `threshold` 只需 0.9（"等于 1 才异常"），
> 不用去纠结 APM 域约定是 0–1 还是 0–100。

**为什么两条线用同一个窗口**：`group_key` 是"**该轮**异常集合"的哈希（`models/fingerprint.py`）——
**集合一变就是另一张单**。窗口同步能保证两条线落在**同一轮**里，这是"合成一条记录"的**前提**。

> ⚠️ **但"同轮"并不自动等于"同一条记录"—— 这一条是实测推翻的（2026-09-29 批次 C）。**
> `pipeline/grouping.py`（M9 起）是**连通分量**分组：日志异常按 signature / traceId 相连，
> 而 **metric 异常只往日志组上挂，彼此之间不连边**。
> 于是**同一个服务的两个 metric 异常、且没有日志时，会分成两组 ⇒ 开出两张单**。
> 实测：注入 CPU 忙循环后拿到 `PR-…-0359`（cpu_usage）与 `PR-…-0360`（cpu_throttled_percent）
> **两条各带一个异常**的记录，人要点两次 Analyze。
>
> **✅ 已修（2026-09-29，当日）**：`grouping.py` 加一条连边 —— **同服务的 metric 异常互相成组**
> （日志三条规则一字未动；15 条既有用例全绿，另补 3 条）。实测第二轮注入拿到
> `PR-20260929-0392`：**一条记录里两个 anomaly**（修复前不可能）。
>
> **⚠️ 但还有第二层，同一轮实验暴露的**：两条线的**越线时刻不同** —— 限流比 **40 秒**就到 100%，
> 而 CPU 占 limit 是**一分钟平均**、要等满窗口。于是先越线的那条**先开一张只有自己的单**
> （`PR-…-0390`），下一轮集合变大 ⇒ `group_key` 不同 ⇒ 再开一张（含两条）。
> **根因不是分组，是去重键**：`write_or_append`（`storage/records.py`）只在
> **`group_key` 完全相同**时追加，而 `group_key` 是**精确异常集合**的哈希 ——
> **集合一变就是另一张单**，这是设计如此（M9 要的就是"不同类型的 error 各自成单"）。
>
> 四条出路（按代价排序）：
> | # | 做法 | 代价 |
> |---|---|---|
> | ① | **两条线门限都抬到"确实饱和"的量级**（症状线 5% → 50%），两条线就会同时越线 | **实测未达成，见下** |
> | ② | 域内 `persistence_rounds: 2` —— 让慢的那条在一轮内追上，任何单都等两轮才开 | +1 轮延迟；且**影响该域三条日志端点**（V11 里有人把它从 2 改成 1，原因未记录） |
> | ③ | `write_or_append` 支持"**超集并入**已开记录"（新集合 ⊃ 某开单集合 ⇒ 并入并更新异常表） | **✅ 已实施并实测通过（见下）** |
> | ④ | 接受一次故障最多两条记录 | 零改动；人可能要点两次 Analyze |
>
> **✅ ③ 已实施（2026-09-29 当日）**：`storage/records.py` 加了 `_superset_candidate` /
> `_merge_anomalies` / `_apply_merge`（InMemory）与 `_find_superset_candidate` / `_merge_into`（PG）。
> 判据是**严格子集**（相等由既有的 `ON CONFLICT` 精确键路径处理）；并入后把 `group_key`
> **提升为超集键**，下一轮同样的集合就走精确键追加，不再读-改-写。
> **实测**（新 pod、两条线仍然差一整分钟越线）：症状线 t+2:20 先建单 → 风险线 t+3:21 越线后
> **并入同一条** ⇒ 全程**只新增 1 条记录**、`metric_anomalies` 里两条、`occurrence_count=36`。
> ⚠️ 已知边界：若同时存在两个**互不包含**的 partial 单（{A} 与 {B}），新的 {A,B} 只能并进其中一条；
> ⚠️ 副作用：并入会改 `group_key`，该记录的 fpr 历史键随之改变（`min_samples=20` 下闸门本就长期失效）。
> ⚠️ 症状线门限**已改回 5**（50 那次实验证伪，理由见 `docker/seed_testbed_metrics.py` 的注释）。
>
> **① 为何实测没达成（2026-09-29 第三、四次注入）**：门限抬到 50% 并不能让两条线同时越线 ——
> **限流比 40 秒就到 99%**（与门限取 5 还是 50 无关），而 CPU 占 limit 是**一分钟平均**、
> 物理上就得等满窗口。两次注入里一次两条线落在同一轮（追加到了已开单，看着像成功），
> 另一次差了整整一分钟（`PR-…-0430` 只有症状线、`PR-…-0432` 两条），**说明它靠运气**。
> 根因是**集合会变**（某条线晚越线、日志异常中途进来/退出），而 `group_key` 是精确集合的哈希 ——
> **外部调参改不掉这一点，只有 ③ 能。**

**所以症状线的价值是"判别有没有真的影响用户"（分诊/定严重度），不是"更快"。**

**门限拿历史数据定，别拍脑袋**：拉一周曲线，选一个"正常业务从没到过、但真实饱和一定到"的位置。
testbed 现成那条 `PodCPUHigh`（95% / 1 分钟）可以当"已经很严重了"的上界参照。

**为什么这里宁长勿短**：本系统的误报代价不是"看一眼"，而是**一整条 LLM 诊断链 + 一个人坐到
Diagnosis Center 前面做裁定**。上游该比普通告警更保守 —— 晚 3 分钟出单，远比每天几条假单便宜。

### 4.3 第三层：成单 —— APM 只做搬运 + 落成一条 problem_record

- **两条线各一条** `monitor_target`：`signal_type=metric` / `source_type=prometheus` /
  `url=<prometheus>/api/v1/query` / `params.query=` 上面那两条之一（风险线 §4.2 / 症状线 §4.2.1） /
  `field_mapping` 取 `value[1]`、`value[0]`、
  `metric.metric`、`metric.service`（pod 标签见下）；`schedule.interval_sec=60`。
- **风险线的检测器不用改**：`application` 域已有 `{signal: cpu_usage, plugin: static_threshold, threshold: 0.9}`，
  对 0/1 输入恰好等价于"等于 1 才异常"。
- **症状线要新增一条 detector**（**必须用不同的指标名**）：
  `{signal: cpu_throttled_percent, plugin: static_threshold, params: {threshold: 0.9}, severity: high}`。
  ⚠️ 别图省事复用 `cpu_usage` 这个名字：那样两条线的 `anomaly_key` 会撞（键是
  `metric|tenant|service|指标名|labels`），**记录里分不出"是 CPU 满还是被限流"**，
  而且两条线会共享同一个持续性计数器。
  ⚠️ 域配置是 **DB 主源**（`loader.py`：DB → 空表 seed → last-known-good），
  光改 `config/domains.yaml` 对已有库无效 —— 要走 `PUT /v1/config/{domain}` 或随 seed 脚本一并 upsert。
- **L3 退化成去抖**（`persistence_rounds=1` 保持不变即可）—— 因为"持续"已经在查询里判完了。
- ⚠️ **一处必须的代码小改**：让 `FieldMapper.map_metric` 从 `metric` 对象取 labels
  （现在硬编码读顶层 `row["labels"]`，见 §3.2），否则 pod 信息在这一层就丢，记录里说不出"是哪个 pod 跑飞"。
  一张单（`group_key` 按 service 归并）仍然是期望行为，**丢的是单内的 per-pod 细节**。
- 成单之后就是现成的：`problem_record(pending)` → Diagnosis Center 列表 → 人点 Analyze。

### 4.4 第四层：解释与取证 —— agent 只答"为什么"

- 从 Diagnosis Center 点 Analyze → 走 `CPU_SATURATION_WORKFLOW_PLAN_zh-CN.md` 那条 metric-first 诊断链。
- agent 的问题**不是**"是否异常"，而是：**形状**（plateau 还是 spike）、**基线**（前一日同时段）、
  **per-pod 分布**、**限流**、**伴随信号**（error_rate / p95）。
- 这两层是互补的：**判定要确定性与便宜，解释要灵活与会追问**。

### 4.5 一张图（四层合起来）

```
K8s ──cAdvisor(5s)──▶ Prometheus ──（★PVC + retention + 清空转 job）──┐
                          ▲                                          │
                          │  ③ 人点 Analyze 时按需取数（形状/基线）    │
                          │                                          ▼
   ① 序列真源              │                              APM 采集器（instant，只搬标量）
                          │                                          │
                          │                     ② 两条线（都在查询里判定，都输出 0/1）
                          │                        症状：被限流 > 5% ｜ 风险：CPU ≥ 80%
                          │                        检测器 0.9（= "等于 1 才异常"）
                          │                                          │
                          │                                 problem_record(pending)
                          │                                          │
                          └──────────────────────────────────────────┤
                                                                     ▼
                                                     Diagnosis Center（列表 → Analyze）
                                                                     │
                                                            ④ 诊断链：rca → plan
                                                                     │
                                                            人工裁定门 → 工单
```

---

## 5. 为什么不用 "Prometheus 规则 + Alertmanager"

那条路更"正统"（规则可复用、静默/抑制/分组都是现成的），但**现在这个系统里它不成立**：

- testbed 里 **没有 Alertmanager**，`prometheus.yml` 也没有 `alerting:` 段 —— 规则算了没人收；
- agentflow 侧**没有告警摄入**（36 条路由里没有 webhook/alerts），APM 的
  `POST /v1/alerts/run` 是"手动跑一轮采集"，**不是接警入口**；
- 走那条路要新写一个入站适配层，而且诊断结论与 `problem_record` 是两套流转。

**什么时候该换过去**：如果生产环境本来就有 Alertmanager，且希望**同一套阈值**既驱动告警又驱动诊断
（面板、静默窗口、抑制规则都能复用），那时把"判定"放在 Prometheus 规则里、
再让 Alertmanager 投递到 APM 才是更对的。届时本文 §4.2 那条查询可以**原样**变成一条规则。
换句话说：**查询是资产，放在哪执行是可替换的。**

---

## 6. 执行清单

> 验证按仓库硬性要求**交产物**（命令 + 退出码 + 关键输出行），不写"已验证"。

### 批次 A：testbed 侧的采集与存储（≈30 分钟，`agentflow-testbed`）

| # | 动作 | 文件 | 验收判据 |
|---|---|---|---|
| A1 | **删掉 `app-resources` job**（它与 `app-metrics` 重复，且三个 target 恒 `up=0`） | `manifests/prometheus/prometheus.yml` | 重载后 `up{job="app-resources"}` **无 target** |
| A2 | **retention 显式化**：`--storage.tsdb.retention.time=15d` | `manifests/prometheus/deployment.yaml` 的 args | `/api/v1/status/flags` 里 `storage.tsdb.retention.time = 15d` |
| A3 | **加 PVC** 并把 TSDB 挂上去 | 同上（+ 新增 PVC manifest） | `kubectl -n order get pvc` 有 `Bound`；Pod 重建后 `query_range` 仍能取到旧数据 |
| A4 | `kube-state-metrics` / `node-exporter`：**要么部署、要么从配置里删** | 同上 | 删了 ⇒ 配置里无此 job；部署 ⇒ `count(kube_pod_info) > 0` |

> A3 是本批次**唯一会丢数据**的动作（换存储路径）：先想清楚要不要备份，或者接受丢掉现有 5 段 block。

#### ✅ 已执行（2026-09-29）

改动落在 `agentflow-testbed`（3 个文件：`prometheus.yml` / `deployment.yaml` / 新增 `pvc.yaml`），
**未提交**。验收产物（命令 + 关键输出行）：

| 判据 | 实测输出 |
|---|---|
| A1 + A4 空转 job 已清 | `count by (job) (up)` ⇒ 只剩 3 个 job：`app-metrics`(3 target) / `kubernetes-cadvisor`(1) / `kubernetes-kubelet`(1) |
| A2 保留期显式生效 | `status/flags` ⇒ `storage.tsdb.retention.time = 15d`（改前是 `0s`） |
| A3 PVC 已挂上 | `kubectl get pvc prometheus-data` ⇒ **Bound**，5Gi，`standard` |
| **A3 数据真的持久**（关键判据） | **删掉 pod**（`delete pod -l app=prometheus`）重建后，**查一个比新 pod 启动早 45 秒的时刻**：`container_cpu_usage_seconds_total` 返回 **43 条序列** —— 这些样本只可能来自 PVC |

**两个后续影响，记在这里**：

1. **换存储路径 ⇒ 新 PVC 从空开始**：旧数据（216MB，含"前一日同时段"那个 block）已备份到
   `acc-aiops-platform-new/.prom-backup-20260929/`（**不在任何 git 仓里**）。
   ⇒ **接下来 24 小时内，"基线窗口前移 24h"会查不到数据**（诊断链里那条基线要等数据攒够）。
   要立刻恢复：停 pod → 挂同一个 PVC 用临时 pod 灌回去 → 再起 pod（约 5 分钟，注意文件属主是
   `nobody`）。**本轮没做**，因为验收判据只要求"新数据扛得住重建"。
2. `PodCPUHigh` 规则仍在 `rule_files` 里、仍在评估，但**依旧不外发**（无 `alerting:` 段、无 Alertmanager）
   —— 已在 `prometheus.yml` 顶部注释里写明，别让人再以为它会通知。

### 批次 B：APM 侧的判定与成单（≈1 小时，`aiops-apm-anomaly-detector`）

| # | 动作 | 文件 | 验收判据 |
|---|---|---|---|
| B1 | 新增可重复执行的 seed（幂等，按 `(service, signal_type)` 判重），播 **两条** metric 端点：`cpu_usage`（风险线，§4.2 那条查询）与 `cpu_throttled_percent`（症状线，§4.2.1 那条查询） | 新增 `docker/seed_testbed_metrics.py` + `Makefile` 加 `seed-testbed-metrics` | 两条端点各跑一次 `POST /v1/monitors/{id}/test`：映射里 **`metric` 分别是 `cpu_usage` / `cpu_throttled_percent`、`service=order-service`、`value ∈ {0,1}`**（出现 `unknown` 即失败） |
| B2 | 给 `application` 域**新增一条 detector**：`{signal: cpu_throttled_percent, plugin: static_threshold, params: {threshold: 0.9}, severity: high}`（**风险线复用现有 `cpu_usage` 那条，不动**） | DB 主源 ⇒ 走 `PUT /v1/config/application` 或随 seed 脚本 upsert；`config/domains.yaml` 同步改一份 | `GET /v1/config/application` 的 detectors 里**同时**有 `cpu_usage` 与 `cpu_throttled_percent` |
| B3 | **让 per-pod 标签传得下去**：`map_metric` 从 `metric` 对象取 labels | `collectors/_field_mapping.py:110` 附近（一行 + 一条测试） | 注入后异常记录里 `labels.pod` 有值 |
| B4 | 确认 L3 不用动（`persistence_rounds` 保持活库现值 **1**，退化成去抖） | 无需改动 | — |

> ⚠️ 先跑 `--dry-run` 或直接调 `/test`（它跑一轮、**不动水位线**，是最快的验证手段）。
> ⚠️ 别拿 `docker/seed.py` 当模板：它写的是 `source_type: "http_metrics"` + `metric_path`，`collector_for` 早就不认了。

#### ✅ 已执行（2026-09-29）

改动落在 `aiops-apm-anomaly-detector`（6 个文件，**未提交**）：新增 `docker/seed_testbed_metrics.py`、
`Makefile` 加 `seed-testbed-metrics`、`settings.testbed_prom_url`、`domains.yaml` 一条 detector、
`_field_mapping._labels()`、`tests/test_field_mapping.py` 四条用例。

| 判据 | 实测输出 |
|---|---|
| B1 两条端点已建 | `make seed-testbed-metrics` ⇒ `created MT-0004 … label=cpu_risk` / `created MT-0005 … label=cpu_throttle` |
| B1 映射正确 | `POST /v1/monitors/{MT-0004,MT-0005}/test` ⇒ `metric='cpu_usage'` / `'cpu_throttled_percent'`、`service='order-service'`、`value=0.0`（CPU 空闲） |
| **B3 per-pod 标签传下去了** | 同上 ⇒ `labels={'metric': …, 'pod': 'order-service-6758d474c9-xn94z', 'service': 'order-service'}` |
| B2 检测器 | `GET /v1/config/application` ⇒ `detectors=[cpu_usage, error_rate, ERROR, cpu_throttled_percent]`（新增条 0.9/high；既有三条**未被覆盖**） |
| B4 L3 未动 | 同上 ⇒ `verify={min_samples: 20, persistence_rounds: 1, false_positive_threshold: 0.6}` |
| 幂等 | 再跑一次 ⇒ `updated MT-0004/0005`（不新建、不换号；domain_config version 4→5） |
| **调度器真的在轮** | `GET /v1/audit/rounds` ⇒ `trace-2617e3ff…`：`target_ids=['MT-0004','MT-0005']`、`signals_count=2`、`anomaly_count=0`、`degraded_sources=[]`、逐 target `status=ok signals=1` |

两条设计意图被实测确认：

- **`signals_count=2`**（每 target 恒定 1 条）⇒ "`>= bool` 让每轮恒定有行"生效，
  于是 `signals_count` 可以直接当**采集健康度指标**（变 0 = 采集坏了，而不是"一切正常"）。
- **`anomaly_count=0`** ⇒ 空闲时**不误报**（0/1 判据的 0 侧正确）。

> ⚠️ **`make lint` 目前是红的，但两处都在 HEAD 里就是红的**（`tests/test_problem_diagnose_decision_api.py`
> 未使用的 `BIZ_TRACE`，`c5cb756` 带的），与本次改动无关；本次动过的 4 个文件单独 lint 全过。
> `make test` = **625 passed / 29 skipped**。

### 批次 C：端到端验收（≈30 分钟）

1. **只注入 CPU**（`fault-inject/scenario1.sh` 的第②段忙循环，**不写磁盘** —— 磁盘满会引入第二个症状）。
2. ≤1 分钟：`query_range` 的 `cpu_percent` 从基线 ~0.3% 升到 ~100%（今天实测空闲态 `min/max/avg = 0.162/0.552/0.304`）。
3. **≤4 分钟**（窗口 3m + 采集间隔 60s）：Diagnosis Center 出现**一条**记录，且
   `detection_type = metric`、**`metric_anomalies` 里同时有 `cpu_usage` 与 `cpu_throttled_percent` 两条**
   （都 = 1）、`labels.pod` 是那个被打满的 pod。
   > 忙循环会同时点亮两条线：打满 limit ⇒ 风险线；CFS 配额不够分 ⇒ **必然被限流** ⇒ 症状线。
   > ⚠️ 若出现**两条**记录（一先一后），说明两条线的窗口没对齐 —— 那正是 §4.2.1 那句
   > "**集合一变就是另一张单**"。
4. **反例也要验一次**：杀掉忙循环后，下一轮两条查询都返回 0 ⇒ **不再新开记录**（证明阈值真的在判）。

#### ✅ 已执行（2026-09-29 14:24–14:33）

| 步骤 | 实测 |
|---|---|
| 1 只注入 CPU | `setsid sh -c "while true" ×2`，注入时两条线**都是 0**、库里**没有一条 metric 类记录**（23 条全是日志类） |
| 2 打满 | 40 秒内 `cpu/limit` → **100%**，限流比 → **100%** |
| 3 两条线翻转 | **症状线 t+2:41**、**风险线 t+3:01**（注入口径：窗口 3m + 里面那个 1m rate ⇒ 有效时延 ≈ 4 分钟；症状线更快是因为限流比**一步就到 100**） |
| 3 开单 | t+3:20~3:40 之间出现 **2 条**记录，`severity=high`、`verification.false_positive_rate=0.0`、`log_anomalies=0`，各带 1 条异常且 **`labels.pod` 是那个被打满的 pod** |
| **⚠️ 未达预期** | **是两条记录（每条一个异常），不是一条** —— 成因见 §4.2.1 的更正框（`grouping` 的连通分量不连 metric↔metric） |
| 4 反例 | 杀掉忙循环后 **40 秒内 CPU 回到 0.2%**、**1 分钟内两条线都回到 0**，且**记录数停在 2 不再涨** ⇒ 阈值真的在判。恢复比预想快：`min_over_time` 只要有一个采样点落到门限下就翻回 0（**慢起快落**，正是想要的"不留残留告警"） |
| 5 收尾 | 精确 `kill -9`（**没重启 pod**，避免 rollout restart 顺带制造启动期日志噪声）；未清 ES（本次只压 CPU、不写磁盘，日志窗口无需清） |

**第二轮（改完 grouping 之后重跑，14:38–14:48）**：

| 观察 | 结果 |
|---|---|
| 分组修复是否生效 | ✅ **是** —— `PR-20260929-0392` 一条记录里 **`cpu_usage` + `cpu_throttled_percent` 两条异常** |
| 一次故障是否只出一条 | ❌ **仍是两条**：`PR-…-0390`（只有症状线，先越线的那一轮）+ `PR-…-0392`（两条） —— 成因见 §4.2.1 的第二个更正框（**去重键是精确集合**，不是分组） |
| 集合稳定后会不会重复开单 | ✅ 不会 —— 覆盖期观察 2 分钟，记录数稳定在 4（同 group_key ⇒ `write_or_append` 追加而非新建） |
| 清理与恢复 | CPU 40 秒内回落、限流 1 分钟归零、无残留进程；`/quotation?orderId=ORD001` → **200** |
| 测试 | `pytest tests/test_grouping.py` **18 passed**（15 既有 + 3 新增）；全量 **628 passed / 29 skipped**；本次动过的文件 ruff 全过 |
5. 收尾：`scenario1-recover.sh`；连续跑场景前清 ES 窗口（`curl -X DELETE :19200/app-logs`，见 `docs/constraints/10`）。

### 批次 D：收口（有时间再做）

- D1 把 B1 的端点固化成 migration（照 V9 的形状，GUC 注入地址 + `ON CONFLICT DO NOTHING`），
  这样**新库 `make migrate` 就有**；seed 脚本保留为"改配置"的入口。
- D2 在 `CPU_SATURATION_WORKFLOW_PLAN_zh-CN.md` 的 §8.1 加一行指向本文（那边的 `source_config` 草图被本文 §4.2/§4.3 取代）。
- D3 §3.7 的两条（`series` 120 点截断、`signal_snapshot` 只写不读）**单开一条**，别混在本次里。

#### ✅ 已执行（2026-09-29）

| # | 结果 |
|---|---|
| **D1** | 新增 `V13__seed_testbed_metric_targets.sql`（两条端点 + 一条检测器补丁），`runner.py` 加 `testbed_prom_url` 的 GUC 注入，`tests/test_migrations.py` 三处计数断言更新 + 一条 **V13 形状守卫**。**验收**：① 一次性 schema `aiops_apm_migtest` 上跑全量迁移 ⇒ `applied 13`、5 条 monitor_target（3 日志 + **2 指标**）、检测器 4 个 ⇒ **新库 `make migrate` 开箱可用**（验完已 DROP）；② 活库 `make migrate` ⇒ `applied 1`，且**一行没被覆盖**（MT-0004/0005 仍是 seed 那份、检测器没被重复追加、version 没跳） |
| **D2** | 早已完成（`CPU_SATURATION_WORKFLOW_PLAN_zh-CN.md` §8.1 那行指向）—— 本轮补上勾 |
| **D3** | 登记为 **`docs/TODO.md` §35「指标链路的四个已知边界」**（① `series` 120 点截断、② `signal_snapshot` 只写不读、③ 超集并入的两个 partial 边界、④ `_merge_into` 缺 PG 真库用例）。**①标为最高优先级**：它会让诊断链静默给出错的形状判断 |

> ⚠️ V13 里的两条 PromQL **不是手抄的** —— 由一个生成脚本从 `docker/seed_testbed_metrics.py`
> 的 `_lines()` 直接取，避免"迁移与 seed 各写一份、慢慢漂开"。
> 但要记住 V9 那条约定：**迁移不可变**，以后改配置走 `make seed-testbed-metrics`（幂等刷新），
> **不要改 V13**。

---

## 7. 风险与不做

| 项 | 说明 |
|---|---|
| **PVC 换存储路径会丢历史** | 见批次 A 的备注；接受即可（testbed 现有数据本就不连续） |
| **`>= bool` 每轮每 pod 写一行信号** | 约 1 行/分钟/pod，`signal_snapshot` 只写不读且无保留 ⇒ 量可接受，但要知道它在长 |
| **3 分钟窗口 = 至少 3 分钟才出单** | 这是刻意的取舍：**宁可晚 3 分钟，也不要一条"1 秒尖刺"的假单**。窗口长度是运维参数 |
| **不做的** | 不引入 Alertmanager；不做平台自巡检（现有唯一周期循环是审批 Sweeper，且 APM 的 scheduler 已经在按端点轮询）；不改 `available_metrics()` 的 5 个指标名；不动 MCP 的 `series` 截断（D3 单开） |
| **本文未验的** | 多副本下 per-pod 标签是否真能传到记录里（B2 之后才能测，testbed 现为 1 副本）；PVC 在 minikube 上的默认 StorageClass 行为 |

---

## 8. 本文事实的核实方式

```bash
# 判定查询（四条写法今天都在 testbed Prometheus 上跑通过）
curl -s --get localhost:19090/api/v1/query --data-urlencode 'query=min_over_time((<ratio>)[3m:30s])'
curl ... 'query=(min_over_time((<ratio>)[3m:30s]) >= bool 90)'          # 返回 0/1 且保留 pod 标签
curl ... 'query=(...) and count_over_time((<ratio>)[3m:30s]) >= 4'      # 守卫
curl ... 'query=(...) and on(pod) (count_over_time(...) >= 4)'          # 按 pod 对齐

# 现状（只读）
# 症状线（限流）：分子分母都要有，且都要带 pod 标签
curl ... 'query=count(container_cpu_cfs_throttled_periods_total)'       # 分子存在
curl ... 'query=count(container_cpu_cfs_periods_total)'                 # 分母存在
curl ... 'query=(min_over_time((<throttled/periods 比值>)[3m:30s]) >= bool 5) and on(pod) (count_over_time((<同上>)[3m:30s]) >= 4)'

# 现状（只读）
curl localhost:19090/api/v1/query --data-urlencode 'query=up'           # app-resources 三个 target 全 up=0
curl localhost:19090/api/v1/query --data-urlencode 'query=count(kube_pod_info)'   # 0 series
kubectl -n order get deploy prometheus -o jsonpath='{.spec.template.spec.volumes}'  # 只有 configMap
kubectl -n order exec <prometheus-pod> -- ls -la /prometheus/data        # 8 个 block，5 天里 4 段
curl localhost:7070/v1/monitors -H 'X-Tenant-Id: default'                # 3 条端点，全是 log
curl localhost:7070/v1/audit/rounds?limit=3 -H 'X-Tenant-Id: default'    # 轮次与 signals_count
```

代码位置：`aiops-apm-anomaly-detector/src/aiops_apm/{collectors/http_metrics.py,collectors/_field_mapping.py,
pipeline/l3_verify.py,scheduler.py}`；`multi-agent-workflow/agentflow/datasource/app_indicators.py:186-217`
与其回归测试 `tests/test_app_indicators.py:168-177`；`aiops-mcp-servers/.../backends/prometheus.py:32,81-96,116,177`；
`agentflow-testbed/manifests/prometheus/{prometheus.yml,rules/order-service.yml}`。
