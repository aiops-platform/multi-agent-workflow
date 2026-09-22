# §4 审批 CAS 与 on_reject

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：动审批节点 / 审批 CAS / 审批超时，或调整人工决策点位置时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

4. **审批 CAS + 终态不可逆 + 时间原子判定**（`statestore/base.py:cas_update_approval`）。
   严禁绕过 CAS 改终态。CAS 除状态谓词外还带 `approval_time_guard` 时间谓词（§8.3.2）：
   approve/reject 仅未超时可批、TIMED_OUT 仅超时后可置。改 SQL 必须保留。

4.1 **审批节点的 `on_reject`（abort | continue，默认 `abort`）**：
   - `abort` → 驳回 = **中止整条 run**（run 判 `failed`，节点状态保留 `rejected`）；
   - `continue` → **不中止**；若图上有 `when: approved == false` 的边就沿它路由，
     没有就只是"下游全部失活、run 照常收敛"（两种都合法，见下）。
     ⚠️ 生产中的 `problem-log-diagnose` 用的就是**没有驳回边**的那一种：它的门是终态节点。
   - **每个审批节点都要显式声明**。不写就是 `abort`——图里若同时写了
     `approved == false → recap` 边，那条边**永远不可达**，等于骗人。
   - 判据看**状态**不看**动作**（`rejected_abort_node()`）：queue 模式下 Worker 是
     `from_checkpoint` 重建后直接 `run()`，**不经过 `approve()`**——写在 `approve()` 里的
     中止逻辑在 queue 模式下**完全不生效**，run 照样报 `done`，只有下游被 SKIPPED。
   - **超时**（`REJECTED_CANCELED`）不走这条——那是 `on_timeout` 的语义，尚未实现。
   - **加载期会拦**：`abort` + `approved == false` 出边 = 矛盾，`Workflow.load_yaml`
     直接报 `WorkflowDAGError`（`_check_on_reject_consistency`）。
     **反方向不查**——`continue` 而没有驳回边是**合法**的（= 驳回后下游全部失活、
     正常收敛），加进去会打掉 4 个既有测试。
   - ⚠️ 但这条校验**只对编写路径生效**，加载冻结快照必须 `strict=False`（见约束 6.1）。

4.2 **审批节点的位置就是"要不要人工拍板"的落点**：
   `plan → fix` 若直连，则 plan 一出 `fix` 立刻改代码——**没有任何决策点**。
   实测踩过（`run_b156f65142`）：plan 自己写着「第 3、4 步落地前必须先核对…否则可能修错位置」，
   而第 3、4 步（`code_fix`）已经跑完了。现在的形状是
   `plan → approve-plan → {fix, approve-remediate → remediate}`。
   **加审批 = 加一个节点 + 改边，不要靠 `when` 去 gate 一大片**。
