# §3 DAG 语义（含 halt / closed / VERDICT / ARTIFACT）

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：改 `core/dag.py`、写或改 workflow YAML、加节点 kind 时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

3. **DAG 语义**（`core/dag.py`）：边带 `when`，join `any|all`，全 INACTIVE → SKIPPED 级联。
   审批节点参与 skip。`rejected-canceled`（审批超时）是正式终态常量 `REJECTED_CANCELED`，
   已入 `TERMINAL`，`_edge_active` 视同 REJECTED（下游拒绝路径可求值）。
   改语义必须同步 `tests/test_executor.py` 的 S-010b 场景。

   ⚠️ **`join` 默认 `any`——多入边节点几乎总是要显式写 `join: all`。**
   踩过两次（`locate`、`rca`）：只要有一条**无条件**入边来自"早一波就绪"的上游
   （如 `triage → know → rca` 里的 `know`），该节点就会**与它其余的取证上游同波并发**
   启动，params 在启动时解析 → 那些上游全部解析成 `None`。
   `rca` 因此**从来没拿到过五维取证摘要**（`docs/TODO.md` §19）。
   判据：**入边是不是都"同一批产出"？是就 `join: all` + `required_edges`。**

3.1 **中断语义（`kind: halt`）**：`halt` 是**执行语义的例外**，四条规则：

   - **halt 一旦执行，其余 PENDING 节点全部 SKIPPED**（`_process_skips` 统一裁决）。
     不要靠"给每条通往诊断链的边加 `when`"来实现中断——漏一条边就前功尽弃（实测踩过）。
   - **当前三个触发点**（两图一致）：`scope.insufficient`（不知道该查谁）/
     `rca.insufficient`（查了但证不出根因）/ `locate.found == false`（定位不到要改的仓库）。
     加新触发点**只需加一条 `when` 边**——不用改 halt 本身（这正是 reason 从触发边取的收益）。
     最后一个触发点是踩坑加的：`locate` 报负证据后若无人拦，`fix` 会**从 plan 的文字里
     猜一个仓库**去改，而工作区是默认全量准备的、猜错也静默通过（`docs/TODO.md` §20）。
   - **halt 的 `reason` / `missing` 取自触发它的入边**（`_halt_output(node)` 读
     `_edge_active` 推出的上游输出），**不要写进 `halt.params`**——那样每加一个触发点
     都得记得回来改它，与"逐条 gate 边"是同一个脆弱面。产出另带 `triggered_by`。
   - 判"中断"用 `halt_triggered()`（判据是**图上的 `kind`**，随 snapshot 冻结、可复现）；
     **不要**用"有没有节点被跳过"——那分不清中断与正常分支（`test.passed == false`
     跳过 review/commit 是设计内行为）。run.status 仍为 `done`，
     `GET /runs/{id}` 另给 `outcome: completed|halted` + `halted_at` + `halt`（**现算不落库**）。
   - halt 节点**不经 runner**（不调 LLM、不重试、不做幂等）。

3.1.1 **收尾语义（`kind: closed`）—— 像 halt，但**不**全局跳过**（2026-09-22 加）

   `closed` 是**工单处理流程的终点标记**：走到它就代表这张工单的处理流程走完了。
   与 `halt` 同为**确定性**节点（不经 runner、不调 LLM），两处必须分清：

   |  | `halt` | `closed` |
   |---|---|---|
   | 触发后其余 PENDING | **全 SKIPPED** | **照常跑**（后面还能接 `recap`） |
   | run 的 `outcome` | `halted` → 前端显示**「需要补充信息」** | `completed`（不参与 outcome） |

   - **别把 `closed` 并进 `halt_triggered()` 或 `_ready_nodes` 的独占块** ——
     那两处判据都是 `is_halt`，`closed` 天然不参与。并进去就变成"走完就跳过后续"，
     而它后面按设计还有节点。
   - **`reason` 收字面量、不收 `$.` 引用**：这是一句静态的话（"本流程不回传原系统"），
     写成引用等于让上游（最后一环往往是模型）决定它 —— 与 halt 的教训**方向相反**，
     halt 的 reason 才必须从触发边上搬（每次不同）。
   - **加它的场景**：流程跑到末尾、但**没有可回传的上游系统**（如手工建的工单）。
     此前那种图挂 `ticket-done`，投递必然 404 → 节点判红 → `on_failure: abort` 中止整条 run。
     **判据：不是投递坏了，是不该把一张没有上游的单送去投递口。**

3.2 **「跑完了」≠「通过了」：结论字段判失败**（`VERDICT_FIELDS`，`executor/dag_executor.py`）

   ```python
   VERDICT_FIELDS = {"tester": "passed", "reviewer": "approved", "ticket-done": "delivered"}
   ```

   - **只认 `is False`**。字段缺失 / `None` **不判** —— 判据必须单边定义，
     否则"agent 没按契约输出"会被误报成"结论不通过"。
   - **判定必须在 `on_failure` 之外**（executor 层）。放 runner 里抛会走 `on_error`，
     而 `on_failure: continue` 会把它转成**负证据**、节点照样标 DONE —— 那正是要避免的。
   - **失败要保留输出**（`WorkflowNodeFailed.output`）。理由链
     `failed → issues → summary → note`，链尾 `note` 是给 `ticket-done` 的
     "为什么不交付"留的。
   - **加一个 agent 就是加一条映射**，别在别处写特判。
   - 这条链的终点是**投不出去必须是红的**（绿着一条没交付的 run 比红着更危险：
     看板会算成已闭环）。完整链路（跨三仓）见 `docs/design-v5.8.md` §13。

   > **副作用节点清单也要同步**：`SIDE_EFFECT_AGENTS` 判据是
   > 「**这个 agent 一旦重跑，外部世界会不会多一次可见的变化**」。
   > `ticket-done` 接上 MCP 写工具后即属此类（否则 resume/重放会**重复投递**）。

3.3 **「声称改了」≠「真改了」：产物字段判失败**（`ARTIFACT_FIELDS`，同文件）

   ```python
   ARTIFACT_FIELDS = {"fix-implementer": "files_changed"}
   ```

   声称了非空产物、而本节点**连一次可能改动工作区的成功调用都没有** → 节点判 FAILED。
   与 §3.2 同族、同一条理由（判定在 `on_failure` 之外、失败保留 `output` 作证据、
   加一个 agent 就是加一条映射）。

   实测（run_fc9e158b55）：沙箱不可达，9 次 `ws_write_file` **全失败**，而 fix agent
   拿读到的源码**手工构造了一份 diff**、填上 `files_changed: [3 个文件]`、返回 `done` ——
   那轮 `ws_git status` 已经回了 `nothing to commit`。**它没有结论字段可判**（§3.2 只管
   tester/reviewer/ticket-done），而 schema `required: ["diff","files_changed"]` 逼它
   编一个 diff 出来。这次是 tester 静态核对文件系统兜住的；没有那一步，run 会绿着、
   什么都没交付。

   - **判据刻意窄**：不逐条核对路径 —— `files_changed` 是模型写的散文，与工具入参的
     path 拼法可以不同，而 `fix` 三个 seed 里都是 `on_failure: abort`，一次误判就中止整条
     run。只认最没有歧义的那种形态：**一次成功的写都没有，却声称改了东西**。
   - **证据只认 `result_state == "success"`**：失败的工具调用在流水里也有一行，那是反证。
   - **判不了就不判**（记 warning）：拿不到流水（mock runner / 该节点没跑过）、或有成功的
     `sandbox_run_shell`（`sed -i` 同样改文件，看不出来）。
   - 证据取自 runner 的**跨 attempt 累计**视图（`peek_tool_calls`）—— `retry` 会覆盖
     明细行，而"写过什么"是累计事实；否则重试后会把**已经改好的**树判成编造。
