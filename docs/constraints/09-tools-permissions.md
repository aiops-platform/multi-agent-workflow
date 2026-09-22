# §9 工具权限与 param 解析

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：动 agent 工具权限、加工具、或改 `resolve_params` 时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

9. **工具权限**（§9.5）：`build_agent` 默认 DONT_ASK + agent 注册工具的 allow 规则。
   没有 allow 规则时 DONT_ASK 下工具全部 DENY（联调踩过：agent 只能靠提示词推理）。
   `build_permission_context` 生成上下文。
   ⚠️ **原写的"租户 deny 规则 M5 接入"是未兑现的承诺**——`sandbox/policy.ToolPolicy`
   全类**零运行期消费方**，`build_permission_context` 只从 tool registry 生成 allow、
   完全不读它，所以**租户 deny 规则从未生效过**（另：`runner.py` 调它时没传
   `tenant_id`，一律落到默认 `"local"`）。见 `docs/TODO.md` §23.3。
9.4 **生产适配器（M6）**：`queue/kafka.py`（kafka-python 双队列）、`statestore/postgres.py`
   （§8.8 完整 PG schema）、`lock/redis.py`（SET NX PX + token 校验防误删，**注意 redis get
   返回 bytes，token 比较需 decode**）。配置驱动切换（config.py）。真实 broker/DB 的故障恢复
   （Kafka 重放 / PG 回滚）需生产环境专项验证（§14）；本地已测 Worker SIGKILL 恢复 +
   消息重放幂等。
9.5 **param 解析（M7 修复的潜伏 bug）**：`dag_executor.resolve_params` 中 `$.nodes.X.output`
   （无字段）与 `.output.field` 都必须正确解析——"output" 是标准访问器，**不能当字段遍历**
   （曾因遍历 `output["output"]` 返回 None，导致所有 workflow params 静默失效；`when` 条件
   用 expressions.get_path 无此问题）。改 params 解析必跑 `test_param_resolution_output_accessor`。
