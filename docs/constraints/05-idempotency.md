# §5 副作用幂等与 checkpoint 一致性

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：加副作用节点、动幂等键、或动 checkpoint 落库时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

5. **副作用幂等**（`executor/idempotency.py` + `executor/dag_executor.py:_external_operation_id`）：
   `execution_id` 唯一 + `external_operation_id` 复用（§8.4.2）。副作用 agent
   （`SIDE_EFFECT_AGENTS`：committer/infra-remediator）自动用 `run_id:node_id` 确定性键；
   YAML 节点可声明 `idempotency_key`（支持 `$.` 引用）做内容键（§8.4.3）。新增副作用
   节点必须加入清单或声明幂等键。
5.1 **checkpoint 一致性**：`update_node_status` 必须同步写 `cp` 列（status/output 列只是
   cp 的冗余投影——GET /runs 读列、Resume 读 cp）。只写列不写 cp 曾导致审批超时后
   Resume 永卡 waiting_approval（回归测试 `test_approval_timeout_resume_converges_sqlite`）。
