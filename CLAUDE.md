# CLAUDE.md — agentflow 后端开发指南

基于 `design-v5.2.md` 的 AIOps Bug Fix 智能体平台后端（M0-M2 脚手架）。

## 开发命令

```bash
make install   # 创建 venv + 安装依赖
make test      # 跑 pytest（M0/M2 语义 + 幂等 + Resume）
make demo      # （已移除，见下方「工作流的真源」）
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

6.0 ⚠️ **工作流的真源是数据库，不是仓库文件**（2026-09-16 起明确）
   - **存**：`POST /workflows` → `workflow_store.py` 的 `INSERT INTO workflows(id,name,yaml,created_at)`
   - **用**：run 时从库读——`api/app.py:697` `cs.workflow.list()` → `:704` `get(wid)`
     → `:398` `Workflow.load_yaml(wf_row["yaml"])`
   - **仓库里的 `workflows/*.yaml` 已于 2026-09-16 删除**，`agentflow/demo.py` 与
     `make demo` 一并删除——它们读的是仓库文件，**会让人以为改 YAML 就生效**。
     实际改了仓库 YAML 而没同步到库时，run 跑的还是旧流程，**且没有任何提示**。
   - **要改 workflow**：`PUT /workflows/{wid}`（或 `POST /workflows` 新建），改完立即生效
     （已发起的 run 不受影响——它们用 snapshot 冻结）。
   - 原设计的 DAG 形态（节点类型 / when / join / 审批门禁）见 `docs/design-v5.6.md` §8.1；
     当前两条流程的节点结构见 `docs/design-v5.7.md` §7.2。
   - **新租户的坑**：`tenantctl provision` 只建库建表、**不播种 workflow**，新租户
     `workflows` 表是空的 → `POST /tickets/{tid}/run` 直接 400。见 `docs/TODO.md` §13。

6.1 **Worker/双队列**（§6/§8.6，`AGENTFLOW_RUN_MODE`）：`inline`（默认，进程内直跑）|
   `queue`（API 只发布 run.trigger.{tenant} / run.command.{tenant}，Worker 消费；
   memory=进程内 WorkerPool 自动接 active 租户，kafka=`python -m agentflow.worker`
   [--tenant <id>] [--dsn postgres://…]）。**--dsn**：容器/共享库直连单租户（管理库
   db_ref 的 localhost DSN 在 k8s 容器不可达）；**--tenant**：只消费该租户 topic。
   executor 一律经 `resume_executor` checkpoint 重建（`load_snapshot_workflow` 的
   `await` 不可删——曾缺失导致 resume 全挂）。queue 模式 approve 只做 CAS+发命令，
   零进程内 executor 依赖。pause=波间暂停。Worker 镜像 `docker/Dockerfile.worker` +
   manifest `deploy/worker-deployment.yaml`（本地 minikube 实操见 docs/DEPLOYMENT §2.3）。
7. **多租户（v5.3 五原则，design-v5.3.md）**：
   - **存储路由**：一切库访问经 `statestore/router.py`——普通 StateStore 包装固定解析
     （既有单库用法/测试零改动），`TenantStoresRouter` 按 tenant_id 路由到租户库
     bundle（**state+workflow+mcp+agent_config+ticket**，LRU 淘汰关闭）。**新读路径
     必须经 `service.store_for(tenant)` / router，禁止绕过**（P4：隔离由构造保证）。
     > **新增控制面表时务必加进 `TenantStores`**。`ticket_store` 起初只作为模块全局
     > 存在、不在 bundle 里 → 所有租户的工单都写进共享库（2026-09-14 修复）。
     > 判据：**模块全局 store 只该出现在 `_GlobalStores` 回退形状里**。
   - **PG 租户库是真的独立 database**（`{基础库}-{tenant_id}`，如 `agentflow-otr`）：
     `provision` 会 `CREATE DATABASE … TEMPLATE template0` 建库，`deprovision
     --confirm-delete` 会 DROP（**拒绝删共享基础库**，防连坐管理库）。
     库名派生与建/删库在 `tenants.py`；**`_default_db_ref` 只有一份实现**——
     早期 tenantctl/tenants/router 各写一份，postgres 分支都返回共享 DSN，
     导致 `workflows`/`mcp_servers`/`agent_configs`（**这三张没有 tenant_id 列**）
     跨租户互相可见。
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
   - **数据面 = 租户 MCP（v5.5 批3 起 MCP-only）**：日志/指标/K8s 查询**全部**由
     `aiops-datasource-mcp-server` 提供（`POST /mcp-servers` 注册 →
     `PUT /agent-configs/{name}` 的 `mcp_server_ids` 绑定），**进程内直连实现已删除**
     （原 `agents/datasources.py`）。本地只读工具仅剩 `locate_code`（CMDB 映射）
     与 `search_knowledge`（占位）。详见 `docs/design-v5.6.md` §3。
   - ⚠️ **例外：`datasource/` 直连 Prometheus**（唯一一处，2026-09 引入）。遗留前端
     Smart Inspection 页面（`service-intelligence-platform-ui/js/app.js`）要的是**瞬时值
     + UI 形状的信封**，而 MCP 侧 `backends/prometheus.py` 是面向 LLM 证据的
     `query_range`（只 5 个领域语义指标，无 instant query、无网络/磁盘 IO）。两者目标
     不同，硬套两头别扭，故显式破例。**裁决边界**：本例外只服务
     `GET /app-indicators` 一个端点；agent 取数一律仍走 MCP。
     **收编时注意**：MCP 侧 `_sel()` 用 `container!="POD"`，在测试床集群上**会算错**
     （sandbox 序列的 `container` 标签是缺失的，该写法会把 pod 级 + sandbox + 应用容器
     三条序列全留下，CPU 接近翻倍）；正确写法是 `container!=""`。
     见 `agentflow/datasource/__init__.py` 与 `app_indicators.build_queries`。
   - **`AGENTFLOW_SHARED_DATASOURCES` 语义已收窄**：内置共享数据源工具没了，此开关
     如今**只剩一个作用**——是否放行 `inputs.repos` 直传（默认 0=封堵）。名称保留是
     为了不破坏既有 .env，新代码请按「repos 直传开关」理解。
   - runner 经 `exec_context.current_tenant`（executor 置位）做 per-tenant MCP
     （mcp_manager 缓存键 (tenant, server_id)，租户间物理不可见）与 per-tenant agent
     配置（agent_config_provider 代际缓存，CRUD 后失效）。
     **注意**：Worker 是独立进程，看不到 API 侧的内存代际计数器，改按**库内指纹**
     判定配置是否变过并热载（`agents/config_sync.py`，`AGENTFLOW_CONFIG_REFRESH_SEC`
     默认 5s）——**绑定新 MCP server 无需重启 worker**（`911c7d3` 修复；早期版本才是
     永久缓存。曾误留作现状描述，2026-09-11 更正）。
   - **生命周期**：`python -m agentflow.tenantctl provision|deploy|upgrade|migrate|
     deprovision`（幂等 saga）；standard 租户专属分支被拒（§9.2 规则 4）；部署记录
     pin SHA 不 pin 分支名。
   - JWT：**鉴权只有两种模式**——`AGENTFLOW_JWT_SECRET` 非空=JWT 模式（claim 派生，
     客户端提交的 tenant 字段一律忽略）；空=dev 回退（`X-Tenant-ID`，告警）。
     租户 claim 优先级 `tenant_id` > `org_id` > `org`；`sub`=审批人身份（JWT 模式下
     approve/reject 的 `by` 也取它，body 不可伪造）。跨租户 run 访问 404
     （`_run_for_tenant`，Router 模式下查不到即 404——隔离由构造保证）。
     **RS256 已可用**（`jwt_algorithm` 是算法无关的 `pyjwt.decode`，把 PEM 公钥放进
     `AGENTFLOW_JWT_SECRET` 即可）；缺的不是算法而是配套：JWKS 自动取钥/轮换、签发侧。
     详见 `docs/E2E_VERIFICATION_zh-CN.md` §JWT 模式。
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
10. **真实数据源 = MCP server**（v5.5）：查询逻辑在
    `aiops-mcp-servers/servers/aiops-datasource-mcp-server`（独立仓库）。要点：
    - **查询必须带时间区间与目标**：`start_time`/`end_time` 必填（ISO8601），窗口由
      workflow `inputs.window_start/window_end` 下发，取数节点 `require` 预检
    - **metric 是领域语义**（cpu_percent / memory_percent / disk_percent / error_rate /
      p95_latency_ms），**不传 PromQL 表达式**；未知值报错并列出可用项
    - ES index `app-logs`（字段 `app.*`，时间 `app.@timestamp`，链路 `app.traceId` 驼峰）；
      Prometheus :19090；kubectl namespace `order`
    - `get_trace` 的**故障 span 启发式**（优先「错误非下游调用症状」的服务=业务根因，
      feign/Read timed out 视为症状）住在 server 侧，属**测试床特定经验**
    - 本地联调：`uv run python -m aiops_datasource_mcp_server`（:8300），再注册/绑定
    trace-analyst 需 `max_iters≥12`（2 个工具 + 链合成，默认 6 会迭代耗尽返回 {}）。
    **场景复现需干净日志窗口**：连续跑两场景会互相污染，切换前
    `curl -X DELETE :19200/app-logs` 清窗。

11. **分层：API ↔ Service ↔ Repository**（2026-09-17 定）。**新增/移动模块前先定它属于哪层。**

    | 层 | 定义 | 判据 |
    |---|---|---|
    | **API** | 带 FastAPI、**经外部 HTTP 访问**的接口 | 文件里真的 `from fastapi import …` |
    | **Service** | **进程内**调用，只做逻辑组装，**不含 FastAPI** | 无 fastapi、无 `api/` |
    | **Repository** | 数据访问 | 被 service 调用，**不反向依赖** |

    调用方向 **API → Service → Repository**，**不可跳层**。

    ⚠️ **判层不能 grep 字符串**——`datasource/*.py` 的 docstring 里就写着 "FastAPI"，
    而它零依赖。看 `import`，不看注释。

    **四条判据**（应当写成测试，比约定可靠）：

    1. `agentflow/api/` **只** import service 与 fastapi
    2. `service/` 与 `repository/` **不得** import fastapi，也**不得** import `api/`
    3. **Worker 必须经 service**，不得直接调 repository / executor
    4. Repository **不得**依赖 service / api

    ⚠️ **当前代码有四处已知违反，别把现状当范本**（整改计划见 `docs/TODO.md` §14）：

    - **（最大）API 层直接编排一切**：`api/app.py`（1600+ 行）import 了 **11 个顶层包**
      —— `agents approval config core datasource executor lock queue service statestore
      tenants worker`，其中包括 `from ..worker import WorkerPool`（`queue=memory` 模式
      下 API 内联拉起 worker，这是设计，但也说明这个文件同时在当端点与当编排层）。
      **它 import 了 `service`，可大量编排逻辑仍写在端点文件里**——新增端点时请把逻辑
      放进 Service，别继续往这个文件里堆。
    - **`api/*_store.py` ×5 是 Repository，却放在 API 层** → `statestore/router.py`
      （Repository）反向依赖 `api/`。今天零代价，但它锁死未来：哪天某个 store 要
      import `app.py` 的东西，**Worker 进程就会被拖上整个 web 栈**（FastAPI/starlette/
      uvicorn），且不会有任何提示。
    - **Worker 绕过 Service**（`worker.py` 直接调 executor + repository），
      **已经造成重复实现**：`service.py:231` 与 `worker.py:212` 各有一份逐行近乎相同的
      `_mark_cancelled`，Worker 那版的 docstring 自己写着「与 RunService.stop_run 同语义」。
      **两处要同步维护**——这就是跳层的实际代价。
    - **`/app-indicators` 由 API 层直接调 `datasource/`**（跳过 Service），
      是已知边界情况，见 `docs/TODO.md` §15。

    > **分层（角色）与进程归属（跑在哪）是正交的两把尺子，同一模块两个答案都要对**：
    > `api/app.py` = API 层 + 仅 API 进程；`service.py` = Service 层 + **两个进程都要**；
    > `statestore/` = Repository + 两个进程都要；`executor/` `agents/` `sandbox/`
    > = 执行引擎（不属三层）+ 仅 Worker。

## 结构速览（v5.3 新增：api/management_store.py、statestore/router.py、tenantctl.py、
exec_context.py、docs/DEPLOYMENT_zh-CN.md）

```
core/        Workflow 模型 + DAG 语义 + 版本冻结（M0）
statestore/  State Model（memory/sqlite/postgres）+ router.py（租户库路由）
executor/    并发 DAGExecutor + 幂等 + Retry + Resume（M2）
agents/      15-agent 编队 + AgentScope 适配 + 工具治理（M1 骨架）
             └ scopes.py       build_permission_context（§9.5 DONT_ASK+allow）
                             （原 datasources.py 已在 v5.5 批3 删除，取数全部走 MCP）
datasource/  ⚠️ 架构例外：Prometheus 直连（仅服务遗留前端 Smart Inspection）
             ├ prometheus.py      薄 HTTP 客户端（查询 + JSON + 并发，零业务语义）
             ├ app_indicators.py  发现→查询→DTO→信封；纯函数与 I/O 分离
             └ service_meta.py    owner/type/agentName ← Deployment 的 aiops/* label
                                  （声明配置非实测值；读失败只留空 + warning，不影响指标。
                                   type 在页面列头显示为 "Squad"；agentName 前端已不展示）
workspace/   WorkspaceManager（M3）；CMDB 已迁 MCP（v5.5.2）
sandbox/     M4：exec 服务(纯 stdlib) + SandboxClient + Orchestrator + ActionExecutor + ToolPolicy
approval/    M5：审批超时 Sweeper + 通知
audit/       M5：审计日志
queue/ lock/ 可插拔队列/锁（memory + kafka/redis 生产适配器）
service.py   RunService：create / approve / resume 编排          ← Service 层（约束 §11）
api/         ⚠️ 按约束 §11 **只应放 API 层**，当前**混入了一层 Repository**：
             ├ app.py / auth.py                     ← API 层（37 端点 / 租户上下文依赖）
             └ management_store.py / workflow_store.py / mcp_store.py /
               agent_store.py / ticket_store.py     ← **实为 Repository，放错了层**：
                 管理库（tenants/schema_versions + db_ref 加密）与四张控制面配置表
                 （**都随租户库走**，TenantStores bundle）。它们被 `statestore/router.py`
                 与 `worker.py` 依赖 —— **反向依赖**，整改见 `docs/TODO.md` §14
tenantctl.py 租户生命周期 CLI（**在顶层**，不在 api/ 下）
workflows/   ⚠️ **已删除**（2026-09-16）——workflow 的真源是数据库，不是仓库文件。
             见下方「工作流的真源」。原设计的 DAG 形态留在 docs/design-v5.6.md §8.1。
scripts/     watch_run.py（run 逐阶段观测）+ mock_mcp_server.py + verify_sandbox.py
docker/sandbox/  沙箱镜像（stdlib-only 离线可建）
```

## 测试

- `tests/test_workflow.py`：加载/冻结/静态校验（§8.2.3/§8.5）
- `tests/test_executor.py`：join/skip/审批 CAS/skip 级联/失败 abort
- `tests/test_resume.py`：SQLite 断点续跑 + RunService 端到端
- `tests/test_idempotency.py`：external_operation_id 复用 / retry / 负证据
- `tests/test_workspace.py`：base_sha 冻结 / 分支隔离 / 幂等 / 无 pull（file:// 本地源）
- `tests/test_app_indicators.py`：PromQL 构造（RE2 转义、`container!=""`、陈旧样本剔除）、
  百分比非有限守卫、status 判定、信封、缓存与降级
- `tests/test_app_indicators_api.py`：`/app-indicators` **不鉴权**（回归锁定）、
  失败仍 200、CORS
- `tests/test_service_meta.py`：Deployment label → owner/type/agentName 映射、
  空值/label 缺失处理、**K8s 读失败不上升**、缓存
- demo 用脚本化 runner（无真实 LLM）；真实模型见 `agents/scopes.py:build_model`

## 里程碑

M0 ✅ → M1 ✅（DB 驱动配置 + MCP 配置化；数据源默认 mock）→ M2 ✅（幂等已接线）→
M3 ✅ → M4 ✅（组件级；API 认证/egress 未落地）→ M5 ✅（CAS 时间谓词）→
M6 🟡（适配器可用，真实 broker/DB 专项待生产）→ M7 🟡（诊断真实，解决侧部分 mock）。
多租户 v5.3 批 A/B/C ✅（管理库+Router+配置表入租户库 / 接单 CAS+topic 租户化+
tenantctl+namespace 派生 / 共享数据源下线+repo 封堵+per-tenant MCP/配置路由，300+ tests）。
待办：MCP 凭证加密+回显脱敏、Mock CMDB 租户维度、Orchestrator 租户 namespace 接线、
Kafka topic 自动建、**JWT JWKS**（算法 RS256 已可用，缺自动取钥/轮换）、
Langfuse/OTel、沙箱 API 认证、真实 Kafka/PG 故障恢复专项（§14）。
