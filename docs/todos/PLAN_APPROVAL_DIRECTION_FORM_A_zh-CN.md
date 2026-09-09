# 修复计划审批：方向决策 Form A（decisions[] + 带方向驳回重写）——实施 TODO

> 状态：**TODO / 设计已定，部分落地**（2026-09-07，分支 `demo_0831`）
> 场景：`remediation-planning-analyst`（修复规划）产出计划后 → 人工审批。审批人要么「一次通过」，
> 要么在互斥修复路径上「选一个方向」。交互形态选定 **Form A**：保留 `kind: approval` 布尔门，
> 把「方向选择」折叠进计划自带的 `decisions[]` + 「带方向驳回重写」反馈，不在引擎里发明新节点类型。
> 关联：`docs/todos/WORKFLOW_V2_REVIEW_GATES_zh-CN.md`（其 §4 `on_reject.rerun` 打回闭环 = 本方案引擎前置）、
> `agentflow/agents/prompts.py`、`agentflow/api/app.py`、`agentflow/agents/agent_config.py`、`tests/test_agent_config.py`、
> `workflows/git-search-approval.yaml`（真实可跑链：diagnose→root-cause→**remediation-planning-analyst→approval**）。

---

## 0. tl;dr

- **已落地（prompt/契约层）**：`remediation-planning-analyst` 的 canonical prompt 加了「一次过审」八规则 +
  方向显式化（`decisions[]` 结构 + `recommended`）；`decisions` 输出 schema 已进 `prompts.py` 并同步到 DB `agent_configs` 行；
  修了一个 resolver 热载未传播到 runner 的潜伏 bug；补了回归测试。
- **未落地（执行/展示层）**：① 审批「驳回」要能**携带所选方向**并**打回重写**（需 §4 引擎扩展）；
  ② SIP Bug Solve 审批卡要能渲染 `decisions[]`（radio 默认选 `recommended`）+ 双态通过/带方向驳回按钮；
  ③ 把 `remediation-planning-analyst → approval` 固化成 workflow（已可跑，走查即可）。

---

## 1. 为什么是 Form A（已定，不返工）

| 形态 | 做法 | 取舍 |
|---|---|---|
| **Form A（选定）** | 审批卡把计划自带的 `decisions[]` 渲染成 radio，**默认选中 `recommended`**；不改任何 radio → 按钮是「✅ 通过·放行计划执行」；改了任一 radio → 按钮变成「↻ 按所选方向驳回重写」，驳回携带 `feedback.decisions=[{decision_id, chosen_option, comment}]` → 重跑 remediation 计划节点（所选即硬约束）→ 回到同一审批节点。 | 方向选择发生在「计划→执行」之间，正好是审批人在场的时点；布尔门语义不变；分歧被显式摆到人前 |
| Form B | 新发明「决策节点」类型，方向即终态 | 与现有 `kind: approval` / when 边 / 审批汇总冲突，成本高 |

**Form A 落地前提 = `WORKFLOW_V2_REVIEW_GATES` §4 的 `on_reject.rerun` 引擎扩展**：当前 `on_reject: abort`，
打回无纠错回路；且 executor 侧打回（非回边）才能避开 DAG 无环校验。

---

## 2. 已完成（DONE，均已验证）

### 2.1 prompt 规则 + decisions schema（`agentflow/agents/prompts.py`）

`remediation-planning-analyst` canonical prompt（规则 1-8）要点：
- 产出可**一次过审**：每一步 `target→action→expected→verification→rollback→risk` 自证，无回滚路径的步骤不允许出现；
- **出稿前先核实**：只引用证据事实，工具可查的项（文件/函数/配置）绝不留进 `open_questions`，没绑代码查询工具就禁止臆测；
- **禁止静默选边**：存在互斥修复路径/需人拍板的取舍 → 显式输出 `decisions[]`（每个含 `options[]` + `recommended` + `accept_criteria`）；
  `recommended` 必须指向 options 某项，审批默认采纳；
- **交付前自检**：终态只输出严格 JSON，对照 schema 检查必填、校验决策自洽；`open_questions ≤ 3`（只留真·产品/外部未知）。

schema 侧：`AGENT_SCHEMAS[name]` 顶层 properties 追加 `decisions`：
`array of {id, question, context, options:[{id,title,description,pros,cons,effort,risk(low/medium/high),rollback}], recommended, accept_criteria}`，
required `[id, question, options, recommended]`。既有契约（`steps.rollback` 必填等）未破坏。
（已程序化断言 `REMEDIATION_PLANNING_SCHEMA == /tmp/rem_schema_v2.json`，即 DB 现状 + decisions，防手抄漂移。）

### 2.2 DB agent 配置同步（`PUT /agent-configs/remediation-planning-analyst` → 200）

行字段：role `fix` / stage `fix` / `system_prompt`=canonical prompt / `output_schema`=含 decisions schema /
`mcp_server_ids=['3cd706d68535']`（只读代码查询 server）/ enabled / reasoning_enabled。
已核对 stored + effective 均含 decisions 契约。

### 2.3 resolver 热载 bug 修复（`agentflow/api/app.py` `_reload_agent_config_resolver`）

**症状**：进程启动后新建/改的 agent 配置不生效——`AgentConfigResolver` 快照在 init 时被 runner 持有一次，
CRUD 热载只重绑了模块全局 + `mcp_manager.server_ids_for`，**没回指 `service.node_runner.agent_config`** →
新配置 agent 用旧快照（NULL prompt → 兜底「你是 AI 运维平台智能体。」）→ 模型工具乱飞后出散文 → `extract_json` → `{}`。

**修复**：热载时若 runner 已注入 resolver，则同步 `runner.agent_config = _agent_config_resolver`（保持同一对象引用，
后续 executor 每节点 `resolve(name)` 都读新快照）。测试：`tests/test_agent_config.py` 新增 2 条。

### 2.4 回归测试（`tests/test_agent_config.py` 末尾 2 条）

- `test_remediation_plan_prompt_has_direction_contract`：prompt 含 `decisions[` / `recommended` / 三条新规；schema decisions items properties 齐全、options 字段齐全；steps 契约不破坏。
- `test_resolve_custom_row_null_prompt_falls_back_to_canonical`：custom 行字段清空（NULL）→ resolve 回退 prompts.py canonical 默认（非通用兜底），origin 保持 custom。

### 2.5 全量测试 & lint

- pytest：46（test_agent_config）+ 68（test_agent_config_api）绿；既有环境基线 6 项（kubernetes/cmdb/workspace）与改动无关。
- ruff：无新增错（仅仓库既有 UP009/I001/F401/S110/RUF100/UP017/UP035/F841 等基线类）。

### 2.6 MEMORY.md

已记录：症状 / 根因（resolver 快照陈旧）/ 修复 / 诊断手法（对比 node_traces 的 llm_call system_prompt vs agent_configs 时间戳 vs 进程启动）/「schema 不运行时强校验、输出契约由 prompt 尾驱动」设计事实。

---

## 3. 待办（TODO，未实现）

### 3.1 后端：审批「带方向驳回 + 打回重写」通道

> 前置 = `WORKFLOW_V2_REVIEW_GATES §4`（`on_reject.rerun`）。Form A 只是把「意见」结构化 = `feedback.decisions`。

- [ ] **执行器**：审批拒绝进入 rerun 时携带 `feedback.decisions=[{decision_id, chosen_option, comment}]`，
      经 `feedback_param` 注入打回段入参 → 重跑 `remediation-planning-analyst`（所选即硬约束）→ 回到同一审批节点；
      `max_iterations` 超限才走终态拒绝 → recap。范围只允许打回上游紧邻重做段（plan 段），不跨到诊断只读段。
- [ ] **API**：`POST /runs/{id}/approve` body 扩展支持显式方向（`decision_overrides` 或缺省=全采纳 recommended），
      reject body 支持 `feedback.decisions` + `comment`；CAS 语义不变（`statestore/base.py:cas_update_approval`）。
- [ ] **契约接线**：runner 重跑时把硬约束注进 remediation 节点入参（如 `constraints: decisions`），
      prompt/`run_agent` 侧无需改（提示词已约定「审批默认采纳 recommended，或低成本改选方向后驳回重写」）。
- [ ] **验收锚点**（对应 §4.5 补测试）：reject 带方向 → plan 节点状态重置并带 feedback 重跑（次数=2）→ 通过后继续；
      连续否决达 `max_iterations` → 终态 rejected → recap；commit 成功后任何打回不触发 commit 重放（幂等锚点回归）；
      Sweeper TIMED_OUT 与打回共存语义明确定义。

### 3.2 前端（SIP `service-intelligence-platform-ui`）：Bug Solve 审批卡渲染 decisions[]（Form A 交互）

- [ ] **审批卡结构化渲染**：`remediation-planning-analyst` 输出的 plan 按区展示——
      summary / root_cause_ref（引用已审批根因）/ approach / **steps 表格**（id·phase·scope·target·action·expected·verification·rollback·risk·requires_approval）/
      impact（affected_services·needs_deploy·change_window）/ risks（risk+mitigation）/ assumptions / open_questions / **decisions[]**。
- [ ] **decisions[] 渲染为 radio 组**：每个 decision 一行：question + options（含 pros/cons/effort/risk/rollback 摘要），
      **默认选中 `recommended`**，标注「推荐」；accept_criteria 作为该行辅助说明。
- [ ] **双态主按钮**：① 全部 radio 保持 recommended（或该计划无 decisions）→ 主按钮 =「✅ 通过 · 放行计划执行」；
      ② 任一 radio 被改选 → 主按钮变「↻ 按所选方向驳回重写」+ 每个被改项旁出现可选 comment 输入 → 提交 = 带方向驳回。
- [ ] 状态轮询沿用 2s `GET /runs/{id}`；重跑后同一审批节点回到 WAITING_APPROVAL，卡内 radio 用新 decisions 重置。
- [ ] 样式沿用既有 `bs-*`/`.bs-node-status-*`；无节点类型/后端契约改动则无需动状态机。

### 3.3 Workflow 固化（可选，走查即可）

- [ ] 现有 `workflows/git-search-approval.yaml` 已是 `diagnose→root-cause→remediation-planning-analyst→approval`，
      把 remediation 计划 + decisions 作为审批卡内容即闭环；如需「决策分歧重写」E2E，等 3.1/3.2 落地后
      在 run 里真跑一次「改选方向 → 驳回 → 计划重写 → 再审批通过」。

---

## 4. 验收走查（落地后全流程）

1. 起后端 `uvicorn agentflow.api.app:app --reload --port 8000`（PG 模式），跑一张真实 ticket；
2. 等 remediation-planning-analyst done，审批卡出现，**decisions 均默认选中 recommended**；
3. 不改 → 通过 → 放行执行（happy path，一次过审）；改一个方向 → 主按钮切「按所选方向驳回重写」→ 提交；
4. 节点打回 plan 重跑（次数=2），输出按所选硬约束收敛，回同一审批节点；
5. 连续驳回到 `max_iterations` → run 终态 rejected → recap，不无限循环；
6. `node_traces` 中该节点 llm_call 的 system_prompt 应为 canonical（非兜底）——回归 2.3 修复。

---

## 5. 备注 / 沿用事实

- 运行期 **schema 不强制校验**：`run_agent` 只 `extract_json(text)`，输出契约由 prompt 尾驱动；
  DB `schema`/`output_schema` 是元数据。故「决策必须显式化」靠 prompt 规则（§2.1），不靠校验器。
- 自定义 demo analyst（含 remediation-planning-analyst）不在静态 15 builtin；其有效配置 = DB `agent_configs` 行（origin=custom）。
  `seed_builtin_agent_configs` 只种 builtin 名 → 该行靠 CRUD 写入，勿清库。
- 改 `prompts.py` canonical 后，DB 行已物化非 NULL → **不自动跟随代码**；需重 PUT 或在 DB 行把字段清 NULL 让其回退。
- 交互形态与引擎前置的取舍细节见 `WORKFLOW_V2_REVIEW_GATES_zh-CN.md §4`（为何不用回边、幂等隔离、状态可见性）。
