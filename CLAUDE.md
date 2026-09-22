# CLAUDE.md — agentflow 后端开发指南

基于 `design-v5.2.md` 的 AIOps Bug Fix 智能体平台后端（M0-M2 脚手架）。

## 开发命令

```bash
make install   # 创建 venv + 安装依赖
make doctor    # ⭐ 环境体检（换机器时先跑这个）；INSTALL=1 时把能装的装上
make test      # 跑 pytest（M0/M2 语义 + 幂等 + Resume）
make demo      # （已移除，见下方「工作流的真源」）
make api       # 控制面 FastAPI（:8000/docs）
make lint      # ruff 检查
```

> **换一台机器、或者别人第一次接手：先 `make doctor`。** 本系统依赖一批**机器相关**的
> 外部件（postgres / kafka / 沙箱 / **gh CLI**），它们的共同点是**缺了不报错** ——
> 只在某条 run 跑到某一步时表现为"结果不对"。`doctor` 把这些问题摆在装环境的时候，
> 并给出可直接粘贴的修复命令（`--install` 能自动装 gh）。
> 判据只有一份：它复用 `tenantctl._env_preflight`，与 provision 时看到的是同一套。

## 每个 task 的收尾（硬性流程）

**一个 task 做完，不要直接进入下一个。按顺序走完这三步再提交：**

1. **Clean code** —— 读一遍自己的 diff，问：
   - 命名/注释与**周围代码**一致吗？（本仓的注释解释"为什么"，不解释"是什么"）
   - 有没有重复实现？本仓已因跳层产生过两份 `_mark_cancelled`（见约束 §11），新增逻辑先找有没有现成的
   - 有没有留下死代码 / 未用的变量 / 只在一处用却抽出来的抽象？
   - 注释里的**事实**是否核实过？（写进注释的断言会被后来人当依据——本仓踩过多次）
2. **安全检查** —— 至少覆盖：
   - **密钥**：新增/改动的字段是不是 `SecretStr`？有没有把凭证打进日志、异常信息、
     `repr`、CLI 输出？（见 `docs/TODO.md` §24；`grep get_secret_value` 可审计取值点）
   - **租户隔离**：新的读路径经 `service.store_for(tenant)` / router 了吗？有没有绕过
     `TenantStores`？（P4：隔离由构造保证）
   - **输入边界**：外部输入（工单字段、MCP 返回、LLM 输出）直接当路径/命令/参数用了吗？
   - **失败姿态**：这条路径失败时是 fail-closed 还是静默降级？**静默降级要显式告警**
     （本仓的 `node_runner=None` 回退、`params` 引用失配返回 `None` 都是这类，见 §23）
   - 拿不准就写进 `docs/TODO.md`，别默默放过
3. **提交** —— 一个 task 一个 commit（见「Git 约定」）。提交信息写**为什么**，
   含验证方式（跑了什么测试、做过变异验证没有）

> 违反这条最容易的形态是"代码写完、测试绿了、直接 commit"——本仓的多数静默缺陷
> （§23 那一族）都不是测试能发现的，而是**这三步里的核对**能发现的。

## Git 约定

- **后端直提 `main`**（本仓不在 PR 流程里）；前端 `service-intelligence-platform-ui`
  提 `vue` 分支。**不开新的 feature 分支**。
- **一个 task 一个 commit**，不要把两件事混进一个 commit。
- 提交信息：首行 `<type>(<scope>): <做了什么>`（`fix` / `feat` / `docs` / `refactor`），
  正文写**为什么**（这是本仓最有价值的部分——多数缺陷的成因比结论更值得留），
  并写明**验证方式**（跑了哪些测试、有没有做变异验证）。
- **push 前先确认工作区干净、全量测试基线没退化**（当前基线见 `docs/TODO.md` §10）。

## 关键设计约束（改代码前必读）

1. **AgentScope 锁定 2.0.3**（design §5）。升级前必须重跑 S-001/S-011；升级后 streaming
   事件 API 可能变化。模型统一走 `agents/config`（DeepSeek `deepseek-v4-flash`）。
2. **基础设施可插拔**：StateStore/Queue/Lock 只通过 `agentflow/statestore|queue|lock/base.py`
   接口访问，配置驱动切换（`config.py`）。本地 InMemory/SQLite，生产 M6 接 Kafka/Postgres/Redis。
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
5. **副作用幂等**（`executor/idempotency.py` + `executor/dag_executor.py:_external_operation_id`）：
   `execution_id` 唯一 + `external_operation_id` 复用（§8.4.2）。副作用 agent
   （`SIDE_EFFECT_AGENTS`：committer/infra-remediator）自动用 `run_id:node_id` 确定性键；
   YAML 节点可声明 `idempotency_key`（支持 `$.` 引用）做内容键（§8.4.3）。新增副作用
   节点必须加入清单或声明幂等键。
5.1 **checkpoint 一致性**：`update_node_status` 必须同步写 `cp` 列（status/output 列只是
   cp 的冗余投影——GET /runs 读列、Resume 读 cp）。只写列不写 cp 曾导致审批超时后
   Resume 永卡 waiting_approval（回归测试 `test_approval_timeout_resume_converges_sqlite`）。
6. **版本冻结**（`core/workflow.py`）：Run 用 `workflow_hash` 复用 snapshot，Resume 只读原 snapshot。

6.1 ⚠️ **静态校验只拦「编写路径」，绝不拦「加载冻结数据」**（2026-09-20 血的教训）
   `DAG.build(..., strict=)` / `Workflow.load_yaml(..., strict=)`：`strict=True`（默认）
   跑全部静态校验，`strict=False` **只跑结构性校验**（环 / 悬空 / join），跳过
   `_check_on_reject_consistency` 这类**内容规则**。

   **三处加载冻结数据的地方必须传 `strict=False`**：
   `api/app.py` 的 run 列表（取 workflow 名）、run 详情（重建图）、
   **`executor/resume.py`（从 checkpoint 恢复）**。

   为什么是硬规则而不是风格：snapshot 是**冻结数据、永远不能被重新编辑**。
   加一条新校验却让它作用到快照上 = **用新规则判旧数据有罪**，后果是
   已经跑过的 run **永久读不出来**。实测代价（加 `on_reject` 校验后）：
   `GET /runs/{id}` 35 个 run 里 **30 个图变空白**，`resume` 直接抛异常。
   而 API 那处 `except: graph = {}` **把异常吞了**，症状表现为"前端图空白"，
   排查方向完全指错（已加 `log.warning(exc_info=True)`）。

   **判据**：这条校验拦的是「**还没保存的图**」还是「**已经跑过的 run**」？
   后者一律 `strict=False`。

6.0 ⚠️ **工作流的真源是数据库，不是仓库文件**（2026-09-16 起明确）
   - **存**：`POST /workflows` → `workflow_store.py` 的 `INSERT INTO workflows(id,name,yaml,created_at)`
   - **用**：run 时从库读——`api/app.py:697` `cs.workflow.list()` → `:704` `get(wid)`
     → `:398` `Workflow.load_yaml(wf_row["yaml"])`
   - **仓库里的 `workflows/*.yaml` 已于 2026-09-16 删除**，`agentflow/demo.py` 与
     `make demo` 一并删除——它们读的是仓库文件，**会让人以为改 YAML 就生效**。
     实际改了仓库 YAML 而没同步到库时，run 跑的还是旧流程，**且没有任何提示**。
   - **要改 workflow**：`PUT /workflows/{wid}`（或 `POST /workflows` 新建），改完立即生效
     （已发起的 run 不受影响——它们用 snapshot 冻结）。
   - 原设计的 DAG 形态（节点类型 / when / join / 审批门禁）见 `design-v5.2.md` §8.1（**仓库上一级目录**，不在 `backend/docs/`——v5.6 §8 是「残余风险」不是这个）；
     当前两条流程的节点结构见 `docs/design-v5.8.md` §4。
   - **新租户的默认数据 = `agentflow/seed/`（种子，2026-09-18 起）**：租户库建好时，
     `TenantStoresRouter._build()` 会往**三张表**写默认数据，让新租户开箱可用——
     `workflows` + `mcp_servers` + `agent_configs`（agent↔server 绑定）。
     只播 workflow 不够：绑定为空 ⇒ **每个 agent 零工具**，run 会跑完但全在空转。
     - **语义：空表才播、绝不覆盖**（每张表各自判断）。所以**改了 seed 文件对已存在的租户
       没有任何效果**——改已开通租户仍走 `PUT /workflows/{wid}`。
     - id 一律 `seed-` 前缀（`save()` 产出 12 位 hex，永不撞），一眼可辨来源。
     - 逃生阀 `AGENTFLOW_SEED_DEFAULTS=0`；数据面 server 地址走
       `AGENTFLOW_MCP_DATASOURCE_URL`（URL 环境相关，不在种子里写死）。
     - 详见 `agentflow/seed/README.md`、`docs/TODO.md` §13。

6.1 **Worker/双队列**（§6/§8.6，`AGENTFLOW_RUN_MODE`）：`inline`（默认，进程内直跑）|
   `queue`（API 只发布 run.trigger.{tenant} / run.command.{tenant}，Worker 消费；
   memory=进程内 WorkerPool 自动接 active 租户，kafka=`python -m agentflow.worker`
   [--tenant <id>] [--dsn postgres://…]）。**--dsn**：容器/共享库直连单租户（管理库
   db_ref 的 localhost DSN 在 k8s 容器不可达）；**--tenant**：只消费该租户 topic。
   ⚠️ **`--dsn` 换的不只是 StateStore**：该 DSN 即 `mcp_servers`/`agent_configs` 所在
   的库，node_runner 要按**同一个 DSN** 把整套 bundle 建出来（`build_tenant_stores_at_dsn`）。
   只换 StateStore 会让 MCP 绑定与 agent 配置一起消失 → agent 零工具。两条路径共用
   `worker.build_node_runner`——**别让任何分支在它之前 return**（曾如此：容器形态下
   每个节点落到 `_default_runner`，不调 LLM、不调工具，而 run 照样报 done）。
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
     与 `search_knowledge`（占位）。详见 `docs/design-v5.8.md` §3。
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
9.6 **沙箱（M4）——写与测试的执行边界**。判据一句话：**谁持有密钥，谁不执行不可信代码。**

   - ⭐ **构建镜像：`make sandbox-image`（换机器第一步，就这一条命令）**

     ```bash
     make sandbox-image          # 建基础镜像 + java21 变体（有先后，见下）
     podman images | grep agentflow-sandbox   # 确认：两个都在
     ```

     | 步骤 | 产物 | 联网 |
     |---|---|---|
     | `docker/sandbox/Dockerfile` | `localhost/agentflow-sandbox:local` | ❌ 纯 stdlib，**离线可建** |
     | `docker/sandbox/Dockerfile.java21` | `localhost/agentflow-sandbox-java21:local` | ✅ apt 装 JDK21（~123MB）+ 下 gradle 发行版 |

     **两步有先后**：变体是 `FROM localhost/agentflow-sandbox:local`，基础镜像不在就建不出来。
     `make` 已经把顺序排好，别手动拆开跑。

     - **容器 CLI 自动探测**（`CONTAINER ?=`，podman 优先否则 docker）——不用先决定用哪个。
     - ⚠️ **`backend/.dockerignore` 不能删**。构建上下文是整个 `backend/`，而三个
       Dockerfile 加起来只 `COPY` 了 `agentflow/` + `docker/sandbox/warmup/` +
       `pyproject.toml` + `README.md`。**没有它，每次构建都要先把 609MB 塞进
       VM/daemon**（其中 `venv/` 独占 375MB）—— 症状是「**构建卡在没有任何输出的
       地方好几分钟**」，极易被误判成网络慢。有了它上下文是 **4.2MB（约 145×）**。
       改它之前先看文件头的三个 Dockerfile 清单，**`COPY` 里出现过的路径一条都不能排掉**
       （`!README.md` 那条负向规则就是为 worker 的 `COPY README.md` 留的）。
     - ⚠️ **镜像名必须带 `localhost/` 前缀**，三个地方是硬绑定的：
       `docker-compose.yml` 的 `sandbox` 服务写死 `localhost/agentflow-sandbox-java21:local`，
       `Dockerfile.java21` 写 `FROM localhost/agentflow-sandbox:local`。
       **不带前缀时 podman 会自动补 `localhost/`、docker 补 `docker.io/library/`** ——
       所以同一句 `docker build -t agentflow-sandbox:local` 在 podman 上一直是对的，
       **在 docker 上会建出另一个名字**，于是 `FROM` 找不到基础镜像、compose 也找不到服务镜像。
       `make` 里固定带前缀，**照 make 跑就不会踩**；README/REPRODUCE 里那几条裸
       `docker build` 是历史写法（已改为指向 `make`）。
     - ⚠️ **耗时以"网络"为主，不以"CPU"为主 —— 实测最坏 86 分钟**。
       基础镜像分钟级。java21 的成本全在**拉那个 ~130MB 的 gradle 发行版**
       （`services.gradle.org`），实测（2026-09-22）反复失败重试了
       `Connection refused` / `timeout (120000ms)` / **下到一半 zip 损坏**
       （`zip END header not found`），前后 **86.5 分钟才建成**，
       而 apt 装 JDK 那步其实是缓存命中的。
       **对策就是重跑**——`make sandbox-image` 是幂等的，建成的层会命中缓存，
       不会从头再来；**别因为慢就去改 apt 镜像源**（实测 deb.debian.org 反而最快）。
       apt 偶发 `Hash Sum mismatch` 同理是常态而非意外（已设 `Acquire::Retries=5`）。
     - **验证**（比 `podman images` 更有说服力，因为它证明缓存真的可用而不仅是"有镜像"）：
       ```bash
       # 干净副本 + --network none：能跑通就说明 gradle 发行版与依赖缓存**确实烤进去了**
       cp -R <工作区>/repos/order-service /private/tmp/sbx-check && rm -rf /private/tmp/sbx-check/build
       podman run --rm --network none -v /private/tmp/sbx-check:/work -w /work \
           localhost/agentflow-sandbox-java21:local ./gradlew test --no-daemon -q
       ```
       实测：**6 个测试全过、全程 4 秒、零网络**（2026-09-22 对 `order-service` 实测）。
       ⚠️ **不要给 `/gradle-home` 挂卷**（见下条），挂了这条验证会失败或退化成几分钟。
       macOS 上路径要用 `/private/tmp/...` 而不是 `/tmp/...`（后者是软链，虚拟机里认不出）。
     - **跳过它的症状不在"沙箱"上**：`docker-compose.yml` 的 sandbox 服务**只声明 `image`、
       没有 `build`**，镜像不在就起不来。于是 `ws_write_file` / `ws_run_tests`
       **fail-closed 报错**（不回退本地执行）→ agent 拿着"沙箱不可用"反复试错、烧完 ReAct
       轮次 → 节点失败 → `fix`/`test` 走**默认的 `on_failure: abort`** → **整条 run 中止、
       工单不回传**。而报错文案里**不会出现"沙箱镜像没建"**这几个字，方向指到别处。
     - **K8s（minikube）还要多一步**：镜像得先塞进集群，否则 Pod `ImagePullBackOff`。
       见 `REPRODUCE.md` 的 `minikube image load` 一段。compose 路径**不需要**这步。
   - **镜像分两层**：`docker/sandbox/Dockerfile`（基础）**永远可离线构建**，只跑 exec
     服务、不含工具链/不含 git/不含密钥；按 runtime 叠加变体
     `Dockerfile.java21`（JDK21 + **烤进镜像的 gradle 缓存**）。**没有 `WITH_JDK`
     构建参数了**——工具链与"零依赖离线可建"是矛盾的，揉在一起会让基础镜像的构建条件看开关。
     Java 服务只装 JDK、不装 gradle：`./gradlew` 自带 wrapper（`java -jar
     gradle-wrapper.jar` 下载，不需要 wget/curl/unzip）。
   - ⚠️ **gradle 缓存必须"烤进镜像"，不能靠运行时挂卷预热**。实测：冷启动跑一次
     `./gradlew test` = **560s（9分20秒）**，而所有超时阀值都是 300s
     → **首次调用必然超时**，tester 拿到 `passed: false` 的**假失败**，然后去"修"一个
     并不存在的测试问题。烤进镜像后**首次 16.9s**（干净副本实测，含真编译真测试、零网络）。
     两个连带约束：① 预热项目 `docker/sandbox/warmup/` 的 `distributionUrl` 必须与
     测试床服务**逐字相同**（wrapper 的缓存路径哈希由 URL 算出，差一字符即白烤）；
     ② **不能给 `/gradle-home` 挂任何卷**（含 Dockerfile 里的 `VOLUME`）——挂载会
     **遮蔽**镜像里那一层，缓存全废、退回 560s。
   - ⚠️ **exec 服务默认只绑 loopback —— 这条是给 K8s 的，compose 下必须显式设
     `SBX_HOST=0.0.0.0`**（已在 `docker-compose.yml` 里设好）。

     `a515ea1` 把默认绑定改成 `127.0.0.1`，理由写在 `exec_service.py` 的 docstring 里：
     「需要它的只有同 Pod 的 worker（走 127.0.0.1），`kubectl port-forward` 打进的是
     Pod 的 loopback」—— **这条推理只对 K8s 成立**。compose + podman(macOS) 下，宿主经
     `ports:` 发布进来的落点是**容器的 eth0 IP**，不是 loopback，于是绑 loopback 的服务
     收不到连接。

     **症状极具误导性**：宿主 **TCP 连得上、HTTP 被 RST**
     （`curl: (56) Recv failure: Connection reset by peer`），而容器**自己的 healthcheck
     照样报 healthy**（它打的是容器内 127.0.0.1）—— 两处结论正好相反。
     实测（2026-09-22）：同一镜像，`SBX_HOST=0.0.0.0` 宿主通、默认 loopback 不通，
     其余条件完全相同。判据一句话：**绑在哪要看"连接从哪来"** ——
     同 netns 来的（K8s sidecar / 容器内健康检查）loopback 就够；跨 netns 来的
     （compose 端口发布）必须绑到 eth0 上。

     ⚠️ **这个坑的隐蔽之处在于"老机器上没事"**：镜像停在改动之前的话，里面的默认值
     还是 `0.0.0.0`，一切正常；**一旦重建镜像就立刻坏掉**，而报错指向沙箱不可达，
     与"镜像"看上去毫无关系。所以它只在新机器/重建后才现形 —— 正是「无感」要挡的那类。
   - **它没有认证**（只要连上就能 POST /exec 执行任意 shell），K8s 下绑 `0.0.0.0`
     会顺着 `hostNetwork` 暴露到节点网络，所以那边**保持 loopback**。
     compose 下绑 `0.0.0.0` 不越界：容器在 podman VM 的 `backend_default` 桥内，
     桥上只有本栈自己的 pg/kafka/redis/sandbox。
   - **worker Pod 加 sidecar + 共享卷**（`deploy/worker-deployment.yaml`），两容器同挂
     `/workspace`；worker 的 `AGENTFLOW_WORKSPACE_ROOT` 必须与沙箱 `SBX_WRITABLE`
     指向**同一挂载点**，否则写校验必失败。沙箱容器**零 `AGENTFLOW_*`**。
   - **哪些工具经沙箱**（`agents/tools.WORKSPACE_SANDBOXED`）：
     `ws_write_file` / `ws_run_tests` **必须经**（它们执行仓库代码）；
     `ws_read_file` / `ws_list_files` **刻意不经**——诊断链的 `code-locator` 靠它们，
     读也依赖沙箱会让沙箱一挂、整条诊断链就跑不起来。`ws_git` 留在 worker（要 PAT，
     且已禁仓库 hook）。**未接线沙箱时那两个工具调用即报错，绝不回退本地执行。**
   - **`AGENTFLOW_TEST_CMDS`（JSON）决定每服务能跑什么命令**，`ws_run_tests` **不接受
     调用方传参**。旧实现让 LLM 传自由命令再拿前缀白名单去猜，而白名单里含 `bash `，
     等于没有白名单。
     ⚠️ **配漏的后果是整条 run 挂掉，而且症状指向别处**：`ws_run_tests` 一调就
     fail-closed 报错，tester 只能改用 `sandbox_run_shell` 自己拼命令去试 ——
     拼对了就过，拼不对就**烧完 16 个 ReAct 轮次** → `AgentOutputError` →
     `test` 节点失败 → `on_failure: abort` 中止整条 run、**工单不回传**。
     实测（run_250bd89c03）：两条路径两次耗尽轮次，token 是正常值的 2.4 倍，
     而错误信息只说"未输出合法 JSON"，看不出是配置问题。
     命令里**不用写 `GRADLE_USER_HOME`** —— `Dockerfile.java21` 已 `ENV` 设成
     `/gradle-home`（缓存烤进镜像，挂卷反而遮蔽它）。实测 `./gradlew test --no-daemon -q`
     在工作区里 17 秒跑完、rc=0。
   - **`ws_git` 一律带 `-c core.hooksPath=/dev/null`**：`git commit` 会执行仓库自带的
     `pre-commit`，而 `.git/hooks/` 也在可写的工作区里——不堵就是"不可信仓库在持有
     全部密钥的 worker 里执行任意代码"。
   - **网络隔离在当前形态下做不到**（sidecar 与 worker 共享 netns；NetworkPolicy 按 Pod
     选且 hostNetwork 下不生效）。分叉与理由见 `docs/TODO.md` §3。
   - ActionExecutor 动作是**有限集合 + 白名单**（§10.3），新增动作需评审。
     ⚠️ **`ToolPolicy`（deny 优先 → allow → 兜底 DENY）目前是空转**：那个类没有任何
     运行期消费方，真正生效的是 `build_permission_context`（只生成 allow、不读它），
     **租户 deny 规则从未生效**。别照着这行写代码，见 `docs/TODO.md` §23.3。
   - SandboxClient 本地联调仍可经 `kubectl port-forward`（打进 Pod loopback，绑
     127.0.0.1 不受影响）。

9.7 **PR 后端（gh CLI）—— 与沙箱同一类：机器相关、缺了静默断链**

   `committer` 的 `ws_open_pr`（`agents/workspace_tools.py`）推分支 + 开 PR，
   它产生 `commit.pr_url` —— **`ticket-done` 判「有没有交付」就靠这个字段**。

   - **凭证不进我们的代码**：`gh` 自己从 keychain 取 token。我们不读、不转发、
     不落任何 GitHub 令牌，所以 worker 进程里**不存在**一个会被日志/异常/`repr`
     带出去的 PAT 变量（§24）。**这也是它留在 worker 而不进沙箱的原因** ——
     持有密钥的一方不执行不可信代码，而沙箱里那份代码不可信。
   - **容器/CI 形态**：`gh` 认 `GH_TOKEN` / `GITHUB_TOKEN` 环境变量，不必交互登录；
     但要把**远端与凭证一起**挂进 worker（`deploy/worker-deployment.yaml`）。
   - ⚠️ **缺了它的症状最阴**：`ws_open_pr` 起不来 → `commit` 节点失败 →
     `on_failure: abort` 把整条 run 中止 → **它下游的 `ticket-done` 根本不执行**，
     原系统那边**什么都收不到**。不是"报了个失败"，是**闭环彻底没有回音**。
   - 所以换机器先 `make doctor`（`--install` 能装 gh）；凭证那步装不了，
     脚本会打出两条路：`gh auth login`（交互）或 `GH_TOKEN=<PAT>`（无人值守）。
   - ⚠️ **`AGENTFLOW_REPO_ROOT` 要用 `https://github.com/<org>` 形式**，不要用本机副本路径。
     工作区是从它克隆的，origin 因此就是工作区的远端：本机路径形态下 origin 是
     `file:///...`，`ws_open_pr` 推得动但**推错地方**（往本机那份副本推），
     而 `gh` 只会说「没有任何 git remote 指向已知 GitHub host」—— **建不出 PR**。
     `ws_open_pr` 现在**先检查 origin 再推**，是 `/`、`file://`、相对路径就直接拒。
   - 三条边界写在 `ws_open_pr` 的 docstring 里：**base 取仓库默认分支**（不让 LLM 挑）、
     **head 取 git 当前分支**（不接受传参，否则能推别的分支上去）、
     **标题/正文走 `--title=` 单参形式**（模型给的值以 `-` 开头时不会被当旗标解析）。

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
agents/      16-agent 编队（9 诊断 + 7 修复）+ AgentScope 适配 + 工具治理（M1 骨架）
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
             见下方「工作流的真源」。原设计的 DAG 形态留在 design-v5.2.md §8.1（仓库上一级目录）。
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
工单回传闭环 ✅（2026-09-21）：结论字段判失败 + MCP 写工具 + APM 接收端点，三仓链路
端到端实测（设计见 `docs/design-v5.8.md` §13；缺口见 `docs/TODO.md` §28–§31）。
待办：MCP 凭证加密+回显脱敏、Mock CMDB 租户维度、Orchestrator 租户 namespace 接线、
Kafka topic 自动建、**JWT JWKS**（算法 RS256 已可用，缺自动取钥/轮换）、
Langfuse/OTel、沙箱 API 认证、真实 Kafka/PG 故障恢复专项（§14）。
