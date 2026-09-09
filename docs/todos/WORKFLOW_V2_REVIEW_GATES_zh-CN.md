# Workflow v2：双人工门 + 打回闭环（设计稿）

> 状态：**设计稿 / 待评审**（2026-09-07，分支 `demo_0831`）
> 范围：仅设计文档 + YAML 定义，**未动引擎代码**。落地上线前需评审 §4 的引擎扩展。
> 关联：`workflows/bug-fix-scenario2.yaml`（M7 可跑闭环，本稿基线）、`workflows/bug-fix-pipeline.yaml`、`workflows/git-search-approval.yaml`、`docs/TODO.md`。

---

## 0. 结论摘要（tl;dr）

**目标流程（人工门后移 + 双门定位化）：**

```
事件 → 诊断(并行取证，负证据) → 根因
  → 计划(fix-planner)
  → 【计划门·人】审核修复计划（带根因上下文）   ← 方向门：审“方案对不对”，还没动代码
  → 实现(fix-implementer)
  → 测试(tester) → review(reviewer)
  → 【证据门·人】审核修复证据（diff+绿测试+review意见） ← 提交门：压在不可逆 commit 前
  → commit → 复盘
```

**两处优化（本次补上）：**

1. **优化1 —— 计划门 + 根因上下文 + 风险分级**：`approve-plan` 展示 `rca 根因结论 + plan 方案`，审批人有背景可判断方向；并支持低风险跳过计划门（fail-closed，缺省按高风险走双门）。
2. **优化2 —— reject 从「abort 终态」改为「带意见打回重做 + 迭代上限」**：计划门被否 → 打回 fix-planner 重拟；证据门被否 / review 不过 → 打回 fix-implementer 返工。**当前引擎不支持（无环 DAG + reject 终态），见 §4。**

**当前引擎可直接跑的是 §2 中「去掉所有 `[扩展]` 标注」的 happy-path 版本**（双门、reject→abort→recap），与 scenario2 语义一致。

---

## 1. 与现有流程 / 你上一版的对比

| 版本 | 修复段顺序 | 人工门 | 评价 |
|---|---|---|---|
| 你上一版 | 计划 → 实现 → **审批实现(未测代码)** → 测试 → review → commit | 审批在测试前 | 审未测代码，双循环 |
| `bug-fix-pipeline.yaml` | fix→**approve-changes(测试前)**→test→review→**approve-commit** | 两道，首道审未测 diff | 同上问题 |
| `bug-fix-scenario2.yaml`(M7) | plan→fix→test→review→**approve-commit** | 一道，已后置 | 已是较优形态，但无计划门、无打回 |
| **本稿 v2** | plan→**审批计划**→fix→test→review→**审批证据**→commit | 两道，首道审方案、末道审证据 | 双门定位化 + 打回闭环 |

**为什么这个顺序对**：

- 人工门是稀缺昂贵资源 → 放在「决策不可逆 / 对外可见」的关口，且给审批人看**证据**而非**承诺**。
- **计划门**审的是方案（未动代码），便宜、早，抓错方向止损；上一版的「审批实现内容」从实现之后挪到实现之前，正是关键修正。
- **证据门**压在所有验证之后、副作用（merge/commit）之前，人看到的是「diff + 绿测试 + review 意见」再签。
- 诊断段（根因后）若还要一道人门，**并入计划门即可**，别在根因门与计划门之间做两次紧邻的人工等待（本稿默认只在计划门带 rca 上下文）。

---

## 2. Workflow 完整定义（目标 YAML）

> 命名候选：`bug-fix-review-v2` → 落库文件 `workflows/bug-fix-review-v2.yaml`。
> 注：`[扩展]` 标记的行是 §4 打回闭环所需、**当前引擎尚未支持**；去掉这些行即当前引擎可加载版本。

```yaml
# ===== v2 闭环：计划门(带根因) → 实现 → 测试 → review → 证据门 → commit（打回闭环）=====
name: bug-fix-review-v2
version: "1.0.0"
description: "闭环 v2：诊断 → 计划门(带根因上下文) → 实现 → 测试 → review → 证据门 → commit"

inputs:
  bug_report: { type: object, required: true }
  repos:      { type: object, required: false }
  high_risk:  { type: boolean, required: false }   # 缺省=高风险走双门（fail-closed），见 §3 风险分级

nodes:
  # ===== 诊断阶段（只读，负证据 continue）=====
  triage:  { agent: triage,        params: { bug: "$.inputs.bug_report" }, on_failure: abort }
  logs:    { agent: log-analyst,   params: { bug: "$.nodes.triage.output.summary" }, on_failure: continue }
  trace:   { agent: trace-analyst, params: { bug: "$.nodes.triage.output.summary" }, on_failure: abort }
  metrics: { agent: metrics-analyst, params: { bug: "$.nodes.triage.output.summary" }, on_failure: continue }
  infra:   { agent: infra-locator, params: { bug: "$.nodes.triage.output.summary" }, on_failure: continue }
  locate:  { agent: code-locator,  params: { bug: "$.nodes.triage.output.summary", target_service: "$.nodes.trace.output.failing_service" }, on_failure: abort }
  know:    { agent: knowledge-lookup, params: { bug: "$.nodes.triage.output.summary" }, on_failure: continue }
  rca:
    agent: root-cause
    params:
      logs: "$.nodes.logs.output.summary"
      trace: "$.nodes.trace.output.summary"
      metrics: "$.nodes.metrics.output.summary"
      infra: "$.nodes.infra.output.summary"
      code: "$.nodes.locate.output.summary"
      know: "$.nodes.know.output.summary"
    on_failure: abort

  # ===== 修复段：计划 → 计划门 → 实现 → 测试 → review → 证据门 → commit =====
  plan:
    agent: fix-planner
    params: { rca: "$.nodes.rca.output" }
    on_failure: abort

  approve-plan:                 # ★ 优化1 · 计划门（方向门）：展示 根因上下文 + 修复方案
    kind: approval
    name: "审核修复计划"
    params:
      rca:  "$.nodes.rca.output"    # 根因结论 → 审批人有背景可判断方向
      plan: "$.nodes.plan.output"   # 修复方案（steps / 影响文件 / 风险 / 回滚）
    approvers: ["lead-engineer"]
    timeout: 3600
    # [扩展·§4] on_reject: { mode: rerun, retarget: plan, feedback_param: feedback, max_iterations: 3 }
    # 当前引擎：on_reject: abort（拒绝 → recap 终态）

  fix:
    agent: fix-implementer
    params:
      plan: "$.nodes.plan.output"
      repo_url: "$.nodes.locate.output.repo_url"
    retry: 1

  test:
    agent: tester
    params: { fix: "$.nodes.fix.output", plan: "$.nodes.plan.output" }
    retry: 1

  review:
    agent: reviewer
    params: { diff: "$.nodes.fix.output.diff" }

  approve-commit:               # ★ 证据门（提交门）：人看 diff + 绿测试 + review 意见
    kind: approval
    name: "审核修复证据并提交"
    params:
      diff:   "$.nodes.fix.output.diff"
      test:   "$.nodes.test.output"
      review: "$.nodes.review.output"
    approvers: ["lead-engineer"]
    timeout: 7200
    # [扩展·§4] on_reject: { mode: rerun, retarget: fix, feedback_param: feedback, max_iterations: 3 }
    # 当前引擎：on_reject: abort

  commit:
    agent: committer
    params: { diff: "$.nodes.fix.output.diff", test: "$.nodes.test.output" }

  recap:
    agent: postmortem
    params: { rca: "$.nodes.rca.output", fix: "$.nodes.fix.output", status: "$.nodes.commit.status" }

edges:
  # 诊断链
  - { from: triage, to: logs }
  - { from: triage, to: trace }
  - { from: triage, to: metrics }
  - { from: triage, to: infra }
  - { from: triage, to: locate }
  - { from: triage, to: know }
  - { from: trace, to: locate }
  - { from: logs,    to: rca }
  - { from: trace,   to: rca }
  - { from: metrics, to: rca }
  - { from: infra,   to: rca }
  - { from: locate,  to: rca }
  - { from: know,    to: rca }
  - { from: rca, to: plan }

  # 修复段（双门；拒绝边当前引擎=终态 recap，[扩展·§4]=打回）
  - { from: plan, to: approve-plan }
  - { from: approve-plan, to: fix,   when: "$.nodes.approve-plan.output.approved == true" }
  - { from: approve-plan, to: recap, when: "$.nodes.approve-plan.output.approved == false" }   # [扩展] → 打回 plan
  - { from: fix, to: test }
  - { from: test, to: review, when: "$.nodes.test.output.passed == true" }
  - { from: test, to: recap,  when: "$.nodes.test.output.passed == false" }                    # [扩展] → 打回 fix
  - { from: review, to: approve-commit, when: "$.nodes.review.output.approved == true" }
  - { from: review, to: recap,          when: "$.nodes.review.output.approved == false" }     # [扩展] → 打回 fix
  - { from: approve-commit, to: commit, when: "$.nodes.approve-commit.output.approved == true" }
  - { from: approve-commit, to: recap,  when: "$.nodes.approve-commit.output.approved == false" } # [扩展] → 打回 fix
  - { from: commit, to: recap }
```

**相对 `bug-fix-scenario2.yaml` 的最小改动**（想快速套用时的 diff 视角）：

1. 新增节点 `approve-plan`（插入在 `plan` 与 `fix` 之间），params 同时带 `rca`（优化1）；
2. 边 `plan→fix` 改为两条：`plan→approve-plan` + `approve-plan→fix (when approved==true)` + `approve-plan→recap (when approved==false)`；
3. `approve-commit` 的 params 追加 `review`（把 review 意见并入证据，供审批人看全）；
4. §4 打回闭环在引擎支持前不落地；落地后把各「拒绝→recap」边替换为 `on_reject.rerun` 配置（不再新增回边，避免 DAG 成环）。

### 2.1 修复段各节点权限拆分（读代码 / 执行命令 / 写副作用，别混成一个「读写」）

> 三类能力分开看：**读代码**（只读工具 / MCP）、**执行命令**（沙箱跑测试等，不改 repo）、**写文件/副作用**（改代码、git 提交等不可逆动作）。
> 结论：只有 `fix-implementer` 需要写；`plan`/`review` 纯只读；`tester` 需要执行但不改 repo；`commit` 是唯一不可逆副作用节点（幂等键）。

| 节点 | 读代码 | 执行命令 | 写文件/副作用 |
|---|---|---|---|
| plan (fix-planner) | ✅ 只读 MCP | ❌ | ❌ |
| approve-plan（人） | — | — | — |
| fix-implementer | ✅ | ✅（沙箱） | ✅（改文件，唯一需要写的） |
| test (tester) | ✅ | ✅（沙箱跑测试，不改 repo） | ❌ |
| review (reviewer) | ✅ 读 diff | ❌ | ❌ |
| approve-commit（人） | — | — | — |
| commit (committer) | — | — | ✅（git commit/push，幂等键） |

**配套的 Agent 配置规则：**

- `fix-planner` 的 `mcp_server_ids` 只绑**只读代码 server**；不配任何写工具 allow。
- `fix-implementer` 绑「读 + 沙箱写」；`tester` 走沙箱执行；`commit` 由引擎注入幂等键（`external_operation_id`）执行，不依赖 agent 工具权限。

**两个易踩的坑：**

1. **只读要落到工具层，不只看 server**：agent 绑定是 server 粒度；若 server 混写工具，ToolPolicy `deny → allow → 兜底 DENY` 会让写工具默认 DENY——干净起见只绑纯只读 server，且别给该 agent 配写 allow。
2. **只读 ≠ agent 一定读得到**：只读工具靠 `readOnlyHint` 在 DONT_ASK 下自动 ALLOW；未标 hint 的需在 `build_permission_context(allow_extra=...)` 显式放行，否则 DONT_ASK + 无 allow = 全 DENY（§9.5 联调踩过），`plan` 会退化成无工具纯文本推理。

---

## 3. 优化1：计划门带根因上下文 + 风险分级（接线）

**已体现在 §2**：`approve-plan.params` 含 `rca` + `plan` —— 审批人一次看到「为什么这么修（根因）→ 打算怎么修（方案）」，不用回到上游翻 rca。

**风险分级（可选接线，替代 §2 修复段第一条边）**：低风险例行修复跳过计划门，只留证据门；高风险保留双门。原则 **fail-closed**：`high_risk` 缺失/未知按高风险走双门。

```yaml
# 替换 §2 中：- { from: plan, to: approve-plan }
# 高风险或未声明 → 走计划门；显式 low（high_risk==false）→ 直达 fix（approve-plan 因入边全 INACTIVE 被 SKIPPED 并级联）
  - { from: plan, to: approve-plan, when: "$.inputs.high_risk != false" }
  - { from: plan, to: fix,          when: "$.inputs.high_risk == false" }
```

落地注意：
- 需确认表达式引擎支持布尔字面量比较（scenario2 已用 `== true`，`!= false` 为同族，验证时补一条用例）。
- `high_risk` 缺省时 `$.inputs.high_risk` 为 None，`None != false` 应为 true → 走计划门（fail-closed）。此语义建议加测试锁住。
- 若不想靠调用方声明风险，后续可由 rca 输出加 `risk` 字段或新增轻量「风险评估」agent，边条件改读 `$.nodes.rca.output.risk`。

---

## 4. 优化2：reject 打回闭环（需引擎扩展，本次不实现）

### 4.1 现状与目标

- **现状**：`on_reject: abort` / 拒绝走 when 边 → `recap` = **终态**。一次人工否决整条 run 作废，没有纠错回路。
- **目标**：拒绝 = **带意见打回对应阶段重做**，设迭代上限，超限才终态。

| 关口 | 被否时打回 | 注入反馈 | 超限终态 |
|---|---|---|---|
| 计划门 `approve-plan` | 重跑 `plan`(fix-planner) | 审批意见 → 新 plan | abort → recap |
| 证据门 `approve-commit` | 重跑 `fix`(fix-implementer) → test → review | 审批意见 → 新 fix | abort → recap |
| review(agent) 不过 | 同证据门，重跑 fix | reviewer 意见 | abort → recap |

### 4.2 为什么不能用「回边」实现

DAG 引擎是**无环校验**（`core/dag.py` 静态校验拒绝环，§8.2）。若用 `review→fix` 回边表示打回会直接成环、workflow 无法加载。因此打回必须在**执行器侧**实现为「重置指定节点状态并重跑」，而不是图里加一条边——审批节点仍是普通节点，拒绝后由 executor 决定是否重跑其 `retarget` 段。

### 4.3 建议的引擎扩展（语法草案）

审批节点新增可选 `on_reject` 对象（向后兼容：缺省 = 现 `abort` 行为）：

```yaml
approve-plan:
  kind: approval
  ...
  on_reject:
    mode: rerun            # abort(默认/现行为) | rerun
    retarget: plan         # 打回起点节点：重置该节点及其下游、该 approval 上游紧邻的重做段
    feedback_param: feedback   # 审批意见写入重跑段的入参键，供 agent 读到 reviewer_comment
    max_iterations: 3      # 重跑次数上限；超限 → 终态拒绝 → recap
```

### 4.4 引擎改动点（供评审）

1. **`core/workflow.py` / `core/dag.py`**：`Node` 增加 `on_reject` 字段（含 rerun 配置），静态校验取值合法、`retarget` 存在且非循环敏感（只允许打回该 approval 的**上游紧邻重做段**，不允许跨到诊断只读段，避免诊断重放）。
2. **`executor/dag_executor.py`**：审批拒绝进入 rerun 时——重置 `retarget` 段节点状态为 pending/active（走既有 checkpoint 覆盖语义），把审批意见注入 `feedback_param` 指向的入参，重新调度；统计该审批的重跑次数，超过 `max_iterations` 走 abort。
3. **幂等与副作用安全（关键）**：打回只重置**未产生副作用**的段（plan/fix 前段在沙箱/分支内，重跑安全）。`commit` 一旦成功绝不因审批打回而重跑（`external_operation_id` 幂等键不变）。`retarget` 上限范围里不应包含 `commit`——证据门 reject 的 `retarget` 是 `fix`，不包含已做的 commit。
4. **状态可见性**：打回时节点留 `rejected` 历史 + 一条审计，避免状态机只看到终态而丢了「谁在何时为何打回」。node_traces 仍只留末次成功 attempt（既有 `replace_node_traces` 语义复用）。

### 4.5 验收锚点（将来实现时补测试）

- `approve-plan` 拒绝 → `plan` 节点状态重置并带 `feedback` 重跑（次数=2）→ 通过后继续 fix；
- 连续拒绝达 `max_iterations` → run 终态 rejected/failed → recap，不无限循环；
- `commit` 成功后任何审批打回**不会**触发 commit 重放（幂等锚点回归）；
- 与 `Sweeper` 超时（TIMED_OUT）路径共存：超时走「拒绝→打回」同一语义或仍终态，需明确定义（建议：TIMED_OUT=放弃该人工关，语义同 reject→打回，但超过 max_iterations 终止）。

---

## 5. 落地清单 / TODO（docs/todos）

**评审确认项**
- [ ] §2 目标流程（双门顺序）确认 —— 计划门展示 rca+plan；证据门展示 diff+test+review；
- [ ] 命名：`bug-fix-review-v2`？落 `workflows/` 还是 DB（Workflow Studio 流程库）？
- [ ] `high_risk` 风险来源：调用方入参 or rca 输出 or 新增风险评估 agent？

**当前即可做（不动引擎）**
- [ ] 把 §2「去掉 [扩展]」版本作为 `workflows/bug-fix-review-v2.yaml` 落库（happy-path：双门、拒绝→recap），E2E 走通 计划门 / 证据门 的 通过→commit 与 拒绝→recap 两路；
- [ ] 补测试：计划门被拒（abort 路径）不调 fix；证据门拒绝不触发 commit（幂等锚点）——对齐既有 S-010b 审批参与 skip 语义；
- [ ] 文档 §3 布尔表达式（`!= false`、None 缺省）若实现风险分级，加回归用例。

**需引擎扩展（§4，单独排期）**
- [ ] `on_reject.rerun` 语法 + 静态校验（§4.4.1）；
- [ ] executor 打回重置 + feedback 注入 + max_iterations（§4.4.2）；
- [ ] commit 幂等隔离回归 + Sweeper TIMED_OUT 与打回共存语义（§4.4.3/§4.4.5）；
- [ ] 打回可见性：节点 rejected 历史 + 审计（§4.4.4）。

---

## 6. 总结

- **流程骨架对且贴合业界最佳实践**：诊断（只读）→ 计划门（审方案，带根因）→ 实现 → 自动测试 → review → 证据门（审 diff+绿测试+意见，压不可逆 commit 前）→ commit。
- **两处优化已并入设计**：① 计划门携带根因上下文 + 风险分级（fail-closed）；② 把「reject=abort 终态」升级为「带意见打回 + 迭代上限」的纠错闭环。
- **当前可直接跑** §2 去掉 `[扩展]` 的 happy-path（双门 + 拒绝→recap），与 `bug-fix-scenario2` 同引擎语义，可先落库 E2E 验证两扇门。
- **打回闭环需引擎支持**（无环 DAG 前提下用执行器侧重跑实现，非回边），已列改动点与验收锚点，建议单独排期，避免与现有 demo 分支耦合。
