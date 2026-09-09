# CLAUDE.md — agentflow 后端开发指南

基于 `design-v5.2.md` 的 AIOps Bug Fix 智能体平台后端（M0-M2 脚手架）。

## 开发命令

```bash
make install   # 创建 venv + 安装依赖
make test      # 跑 pytest（M0/M2 语义 + 幂等 + Resume）
make demo      # 脚本化跑通 bug-fix-pipeline 全链路
make api       # 控制面 FastAPI（:8000/docs）
make lint      # ruff 检查
```

## 关键设计约束（改代码前必读）

1. **AgentScope 锁定 2.0.3**（design §5）。升级前必须重跑 S-001/S-011；升级后 streaming
   事件 API 可能变化。模型统一走 `agents/config`（DeepSeek `deepseek-v4-flash`）。
2. **基础设施可插拔**：StateStore/Queue/Lock 只通过 `agentflow/statestore|queue|lock/base.py`
   接口访问，配置驱动切换（`config.py`）。本地 InMemory/SQLite，生产 M6 接 Kafka/Postgres/Redis。
3. **DAG 语义**（`core/dag.py`）：边带 `when`，join `any|all`，全 INACTIVE → SKIPPED 级联。
   审批节点参与 skip。`rejected-canceled`（审批超时）是正式终态常量 `REJECTED_CANCELED`，
   已入 `TERMINAL`，`_edge_active` 视同 REJECTED（下游拒绝路径可求值）。
   改语义必须同步 `tests/test_executor.py` 的 S-010b 场景。
4. **审批 CAS + 终态不可逆 + 时间原子判定**（`statestore/base.py:cas_update_approval`）。
   严禁绕过 CAS 改终态。CAS 除状态谓词外还带 `approval_time_guard` 时间谓词（§8.3.2）：
   approve/reject 仅未超时可批、TIMED_OUT 仅超时后可置。改 SQL 必须保留。
5. **副作用幂等**（`executor/idempotency.py` + `executor/dag_executor.py:_external_operation_id`）：
   `execution_id` 唯一 + `external_operation_id` 复用（§8.4.2）。副作用 agent
   （`SIDE_EFFECT_AGENTS`：committer/infra-remediator）自动用 `run_id:node_id` 确定性键；
   YAML 节点可声明 `idempotency_key`（支持 `$.` 引用）做内容键（§8.4.3）。新增副作用
   节点必须加入清单或声明幂等键。
5.1 **checkpoint 一致性**：`update_node_status` 必须同步写 `cp` 列（status/output 列只是
   cp 的冗余投影——GET /runs 读列、Resume 读 cp）。只写列不写 cp 曾导致审批超时后
   Resume 永卡 waiting_approval（回归测试 `test_approval_timeout_resume_converges_sqlite`）。
6. **版本冻结**（`core/workflow.py`）：Run 用 `workflow_hash` 复用 snapshot，Resume 只读原 snapshot。
6.1 **Worker/双队列**（§6/§8.6，`AGENTFLOW_RUN_MODE`）：`inline`（默认，进程内直跑）|
   `queue`（API 只发布 run.trigger / run.command，`worker.Worker` 消费；memory=进程内
   Worker，kafka=`python -m agentflow.worker`）。executor 一律经 `resume_executor`
   checkpoint 重建（`load_snapshot_workflow` 的 `await` 不可删——曾缺失导致 resume
   全挂，此前测试未覆盖该路径）。queue 模式 approve 只做 CAS+发命令，零进程内
   executor 依赖。pause=波间暂停（`request_pause` → run() 返回 "paused"）。
7. **多租户（v5.3 五原则，design-v5.3.md）**：
   - **存储路由**：一切库访问经 `statestore/router.py`——普通 StateStore 包装固定解析
     （既有单库用法/测试零改动），`TenantStoresRouter` 按 tenant_id 路由到租户库
     bundle（state+workflow+mcp+agent_config 同库异表，LRU 淘汰关闭）。**新读路径
     必须经 `service.store_for(tenant)` / router，禁止绕过**（P4：隔离由构造保证）。
   - **管理库**（`api/management_store.py`）：tenants/schema_versions；db_ref Fernet
     加密（AGENTFLOW_SECRET_KEY，缺省从 jwt_secret 派生并告警）；**任何 API 不回显
     DSN**。tenants.yaml 仅 bootstrap 种子（async_bootstrap_tenants），运行时以
     管理库为准（TenantRegistry.from_management，CRUD 后重建）。
   - **审批 default-deny（§4.2）**：租户配置过 approvers 后，未命中节点 id 且无 "*"
     → 一律 403（堵"自建 workflow 换审批节点 id 绕过"）；service._check_approver
     先看 `cfg.approvers` 是否非空（空=不限制 dev 语义）。
   - **接单 CAS**：Worker trigger（queued→running）/resume（paused|waiting→running）
     经 `cas_update_run_status` 原子转换，重复消息恰一个接单；新状态机含 queued/paused。
   - **topic-per-tenant**：发布一律 `topic_trigger(tenant)/topic_command(tenant)`；
     Worker 绑定租户只消费自己的 topic，None=全局兜底（单租户回退）。
   - **数据面姿态**：`AGENTFLOW_SHARED_DATASOURCES=0`（默认加固）→ toolkit 不注入
     内置 L1 数据源工具（诊断数据工具一律租户 MCP 绑定）+ inputs.repos 直传 400
     （testbed 联调脚本需显式 =1）。runner 经 `exec_context.current_tenant`（executor
     置位）做 per-tenant MCP（mcp_manager 缓存键 (tenant, server_id)，租户间物理不可见）
     与 per-tenant agent 配置（agent_config_provider 代际缓存，CRUD 后失效）。
   - **生命周期**：`python -m agentflow.tenantctl provision|deploy|upgrade|migrate|
     deprovision`（幂等 saga）；standard 租户专属分支被拒（§9.2 规则 4）；部署记录
     pin SHA 不 pin 分支名。
   - JWT：`AGENTFLOW_JWT_SECRET` 非空=JWT 模式（claim 派生，客户端提交忽略）；
     空=dev 回退（告警）。跨租户 run 访问 404（`_run_for_tenant`，Router 模式下
     查不到即 404——隔离由构造保证）。RS256 暂缓（v5.3 §12）。
8. **Git 版本冻结**（§4.6/§8.7）：`workspace/manager.py` 明确不提供 git_pull；Run 期间工作区
   HEAD 必须 == base_sha，漂移报 `FrozenVersionMismatch`。每个 Run 用 `aiops/RUN_{run_id}` 分支隔离。
9. **工具权限**（§9.5）：`build_agent` 默认 DONT_ASK + agent 注册工具的 allow 规则。
   没有 allow 规则时 DONT_ASK 下工具全部 DENY（联调踩过：agent 只能靠提示词推理）。
   `build_permission_context` 生成上下文；租户 deny 规则 M5 接入。
9.4 **生产适配器（M6）**：`queue/kafka.py`（kafka-python 双队列）、`statestore/postgres.py`
   （§8.8 完整 PG schema）、`lock/redis.py`（SET NX PX + token 校验防误删，**注意 redis get
   返回 bytes，token 比较需 decode**）。配置驱动切换（config.py）。真实 broker/DB 的故障恢复
   （Kafka 重放 / PG 回滚）需生产环境专项验证（§14）；本地已测 Worker SIGKILL 恢复 +
   消息重放幂等。
9.5 **param 解析（M7 修复的潜伏 bug）**：`dag_executor.resolve_params` 中 `$.nodes.X.output`
   （无字段）与 `.output.field` 都必须正确解析——"output" 是标准访问器，**不能当字段遍历**
   （曾因遍历 `output["output"]` 返回 None，导致所有 workflow params 静默失效；`when` 条件
   用 expressions.get_path 无此问题）。改 params 解析必跑 `test_param_resolution_output_accessor`。
9.6 **沙箱（M4）**：`sandbox/exec_service.py` 是**纯 stdlib http.server**（镜像零 pip 依赖，
   离线可建；加 Java 用 `--build-arg WITH_JDK=1`，默认关）。SandboxClient 本地联调经
   `kubectl port-forward`（macOS 宿主不可路由 pod IP；生产 Worker 在集群内直连 ClusterIP）。
   ActionExecutor 动作是**有限集合 + 白名单**（§10.3），新增动作需评审。
   ToolPolicy：deny 优先 → allow → 兜底 DENY（§9.5）。
10. **真实数据源**（testbed 联调）：`datasources.py` 的 adapter 与 mock 工具签名一致
    （SCENARIOS §5.2），数据源切换只换 adapter。ES index `app-logs`（字段是 `app.traceId`
    驼峰，不是 `trace_id`）、Prometheus cAdvisor（`container_*`）、kubectl namespace `order`。
    `get_trace`：ES 按 traceId 重建调用链判故障 span（testbed 的 traceId 未跨服务共享，
    无 traceId 回退时间窗）；**故障 span 启发式**：优先「错误非下游调用症状」（feign/
    Read timed out/Connection refused 视为症状）的服务=业务根因。联调脚本
    `scripts/diagnose_scenario{1,2}.py`（需 `source ../spike/.env` 供 DEEPSEEK_API_KEY）。
    trace-analyst 需 `max_iters≥12`（2 个工具 + 链合成，默认 6 会迭代耗尽返回 {}），
    prompt 已强化区分「业务根因 vs 下游调用症状」。**场景复现需干净日志窗口**：
    连续跑两场景会互相污染，切换前 `curl -X DELETE :19200/app-logs` 清窗。

## 结构速览（v5.3 新增：api/management_store.py、statestore/router.py、tenantctl.py、
exec_context.py、docs/DEPLOYMENT_zh-CN.md）

```
core/        Workflow 模型 + DAG 语义 + 版本冻结（M0）
statestore/  State Model：InMemory / SQLite（M0）
executor/    并发 DAGExecutor + 幂等 + Retry + Resume（M2）
agents/      15-agent 编队 + AgentScope 适配 + 工具治理（M1 骨架）
             ├ datasources.py  真实数据源适配（ES/Prometheus/kubectl，testbed）
             └ scopes.py       build_permission_context（§9.5 DONT_ASK+allow）
workspace/   WorkspaceManager + CMDB（M3）
sandbox/     M4：exec 服务(纯 stdlib) + SandboxClient + Orchestrator + ActionExecutor + ToolPolicy
approval/    M5：审批超时 Sweeper + 通知
audit/       M5：审计日志
queue/ lock/ 可插拔队列/锁（memory + kafka/redis 生产适配器）
statestore/  StateStore（memory/sqlite + postgres 生产适配器）
service.py   RunService：create / approve / resume 编排
api/         控制面 FastAPI
workflows/   bug-fix-pipeline.yaml（§8.1）+ bug-fix-scenario2.yaml（修复闭环）
scripts/     diagnose_scenario{1,2}.py + run_fix_loop.py（修复闭环E2E）+ verify_sandbox.py
docker/sandbox/  沙箱镜像（stdlib-only 离线可建）
```

## 测试

- `tests/test_workflow.py`：加载/冻结/静态校验（§8.2.3/§8.5）
- `tests/test_executor.py`：join/skip/审批 CAS/skip 级联/失败 abort
- `tests/test_resume.py`：SQLite 断点续跑 + RunService 端到端
- `tests/test_idempotency.py`：external_operation_id 复用 / retry / 负证据
- `tests/test_workspace.py`：base_sha 冻结 / 分支隔离 / 幂等 / 无 pull（file:// 本地源）
- demo 用脚本化 runner（无真实 LLM）；真实模型见 `agents/scopes.py:build_model`

## 里程碑

M0 ✅ → M1 ✅（DB 驱动配置 + MCP 配置化；数据源默认 mock）→ M2 ✅（幂等已接线）→
M3 ✅ → M4 ✅（组件级；API 认证/egress 未落地）→ M5 ✅（CAS 时间谓词）→
M6 🟡（适配器可用，真实 broker/DB 专项待生产）→ M7 🟡（诊断真实，解决侧部分 mock）。
多租户 v5.3 批 A/B/C ✅（管理库+Router+配置表入租户库 / 接单 CAS+topic 租户化+
tenantctl+namespace 派生 / 共享数据源下线+repo 封堵+per-tenant MCP/配置路由，268 tests）。
待办：MCP 凭证加密+回显脱敏、Mock CMDB 租户维度、Orchestrator 租户 namespace 接线、
Kafka topic 自动建、JWT RS256（暂缓）、Langfuse/OTel、沙箱 API 认证、
真实 Kafka/PG 故障恢复专项（§14）。
