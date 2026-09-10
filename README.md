# agentflow — AI 运维 Bug Fix 智能体平台后端

基于 `design-v5.2.md` 的 AIOps Bug Fix 智能体平台：DAG 编排引擎 + 15-agent 编队 +
审批工作流 + per-tenant 多租户架构（独立库/队列/namespace/代码分支，design-v5.3，268 tests）。

## 里程碑状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M0** | Workflow 版本冻结 + DAG 语义（join/skip）+ State Model | ✅ 已实现（见 `agentflow/core` + `statestore`） |
| **M1** | AgentScope 适配层（2.0.3 锁定）+ 15-agent 编队 + 工具治理 | ✅ DB 驱动 agent 配置 + MCP 配置化（`agent_config.py` / `mcp_manager.py`）；数据源侧默认 mock，真实联调走 scripts |
| **M2** | DAG Executor + Node Attempt + Retry + Resume | ✅ 幂等已接线（`external_operation_id` 真实生效），含复杂拓扑补测 |
| **M3** | Workspace（Git base_sha 冻结 + 分支隔离）+ CMDB 驱动 | ✅ 已实现（见 `workspace/`） |
| **M4** | Sandbox（独立 Pod + 安全基线）+ Tool Policy + Action Executor | ✅ 组件已实现并经 K8s 端到端验证；**API 认证 / egress 白名单未落地**，L2 工具未接入真实 run |
| **M5** | Approval（CAS + Timeout + Sweeper）+ Notification + Audit Log | ✅ CAS 含 §8.3.2 时间谓词；审批超时→Resume 收敛（已修复卡死 bug）；通知为 log 占位 |
| **M6** | 生产适配器（Kafka/PostgreSQL/Redis）+ 故障恢复专项测试 | 🟡 适配器代码可用（PG 已修复可跑通）；Kafka consumer 多 topic 已修；**真实 broker/DB 故障恢复专项待生产验证（§14）** |
| **M7** | 场景2 修复闭环 E2E（诊断→修复→审批→PR）+ Regression | 🟡 诊断链路（8 agent + DeepSeek + testbed）真实通过；解决侧 fix/review/PR 部分为脚本 mock |

> AgentScope 版本**锁定 2.0.3**（design §5）。升级前必须重跑 S-001/S-011。
> 环境注意：`mcp` 锁定 `<2.0`，拉取后需 `pip install -e ".[dev]"` 重装依赖。

## 快速开始

```bash
# 1. 安装
make install                     # 或 ./venv/bin/pip install -e ".[dev]"

# 2. 配置（模型 + 基础设施后端）
cp .env.example .env            # 填 DEEPSEEK_API_KEY（design §16.3 模型 deepseek-v4-flash）

# 3. 跑测试（268 tests：DAG 语义 + 幂等 + Resume + Worker/队列 + 多租户路由/隔离）
make test

# 4. 跑脚本化 demo（create_run → 审批 → done）
make demo

# 5. 控制面 API
make api                        # http://localhost:8000/docs
```

## 执行模式与多租户（design-v5.3）

```bash
# ── 执行模式（§6/§8.6）──
AGENTFLOW_RUN_MODE=inline   # 默认：API 进程内直接执行 DAG（本地 MVP）
AGENTFLOW_RUN_MODE=queue    # API 只发布 run.trigger.{tenant} / run.command.{tenant}，Worker 消费：
                            #   queue=memory → API 启动时自动拉起进程内 WorkerPool（按管理库租户热接入）
                            #   queue=kafka  → 每租户 Worker Deployment（镜像 {tenant}-{sha}）
                            #                  本地开发：python -m agentflow.worker --tenant <id> [--dsn …]
                            #                  宿主连 kafka：AGENTFLOW_KAFKA_BOOTSTRAP=localhost:19092

# ── 多租户认证（§9.1）──
AGENTFLOW_JWT_SECRET=...    # 非空 = 强制 Bearer JWT，tenant 由 claim（tenant_id/org_id）
                            #   派生，客户端提交的 tenant 一律忽略
                            # 为空 = dev 模式：回退 X-Tenant-ID 头 / 请求体传参（仅联调）
AGENTFLOW_SECRET_KEY=...    # 租户库 db_ref / 凭证 Fernet 加密（缺省从 jwt_secret 派生并告警）

# ── 存储与租户（v5.3 §5：per-tenant DB + 管理库）──
AGENTFLOW_STATE_STORE=sqlite   # sqlite=每租户一个 data/tenants/{tenant}.db；postgres=按 isolation_level 分级
AGENTFLOW_TENANTS_FILE=tenants.yaml   # 仅 bootstrap 种子：首启导入管理库，运行时以管理库为准

# ── 数据面姿态（§7/P1）──
AGENTFLOW_SHARED_DATASOURCES=0  # 0=加固（默认）：不注入内置 ES/Prometheus/kubectl 工具（数据工具
                                #   一律租户 MCP 绑定），并封堵 inputs.repos 直传（400）
                                # 1=dev/testbed 联调：注入共享数据源工具 + 允许 inputs.repos
```

```yaml
# tenants.yaml（bootstrap 种子）示例：首启导入管理库，之后用 tenantctl / 管理库维护
tenants:
  team-alpha:
    isolation_level: standard        # strong=独立库+专属分支允许；standard=共享库+跟 main
    max_concurrent_runs: 5
    workers: 2                       # 租户级 Worker 消费并发数
    approvers:
      approve-changes: ["alice@company.com"]   # default-deny：未命中节点（无 "*"）→ 403；超限 → 429
```

租户生命周期（幂等 saga，详见 `docs/DEPLOYMENT_zh-CN.md`）：

```bash
python -m agentflow.tenantctl provision team-a --isolation strong --branch tenant/team-a --quota 5 --workers 2 --k8s
python -m agentflow.tenantctl deploy   team-a --sha <sha>      # pin SHA（§9.2 规则 2，审计不可变）
python -m agentflow.tenantctl migrate                        # 迁移扇出（按 (tenant, pinned_sha) 记录）
python -m agentflow.tenantctl deprovision team-a --confirm-delete
```

## 目录结构

```
agentflow/
├── config.py          # LLM + StateStore/Queue/Lock + run_mode + JWT 后端切换（配置驱动）
├── core/              # M0：Workflow 模型 + DAG 语义（join/skip）+ 版本冻结
├── statestore/        # M0/M6：State Model（InMemory / SQLite / PostgreSQL，表结构对齐 §8.8）
├── queue/ lock/       # M0/M6：可插拔队列/锁（memory + kafka/redis 生产适配器）
├── executor/          # M2：并发 DAG Executor + 幂等 + Retry + Resume + 波间暂停 + 租户上下文
├── worker.py          # §6/§8.6：Worker/WorkerPool（消费 run.trigger.{tenant}；
│                       #   python -m agentflow.worker [--tenant <id>] [--dsn postgres://…] 单库直连）
├── tenantctl.py       # v5.3 §10：租户生命周期 CLI（provision/deploy/upgrade/migrate/deprovision）
├── agents/            # M1：15-agent 编队 + AgentScope 适配 + 工具治理 + 权限上下文
│   ├── agent_config.py    # DB 驱动 agent 配置解析（DB 覆盖 + 内置回退合并）
│   ├── mcp_manager.py     # MCP server 连接管理（stdio/http + 热刷新 + per-tenant 缓存与绑定）
│   ├── runner.py          # AgentNodeRunner：真实 LLM 接入 DAGExecutor（按 current_tenant 租户路由）
│   ├── transcript.py      # 节点级 LLM 对话/工具调用明细采集 → node_traces
│   └── datasources.py     # 真实数据源适配（ES/Prometheus/kubectl，testbed 联调）
├── workspace/         # M3：WorkspaceManager（base_sha 冻结/分支隔离/无 git_pull）+ CMDB
├── sandbox/           # M4：exec 服务(纯 stdlib) + SandboxClient + SandboxOrchestrator + ActionExecutor + ToolPolicy
├── approval/          # M5：审批超时 Sweeper（§8.9）+ 通知
├── audit/             # M5：审计日志（§9.5 字段 + 输入脱敏）
├── tenants.py         # v5.3 §5.2：租户配置（default-deny 审批白名单；管理库驱动，yaml 仅 bootstrap）
├── api/               # 控制面 FastAPI（27 端点 + JWT 租户派生 + sweeper 后台任务）
│   ├── auth.py            # §9.1：JWT → 派生 tenant_id（get_tenant_context 依赖）
│   └── management_store.py # v5.3 §5.2：管理库（tenants/schema_versions，db_ref 加密）
├── statestore/router.py # v5.3 §5.3：TenantStoresRouter（tenant_id → 租户库 bundle，LRU）
└── service.py         # RunService：create / approve / resume / pause / stop（inline|queue 双模式）
workflows/
├── bug-fix-pipeline.yaml   # design §8.1 完整示例
└── bug-fix-scenario2.yaml  # 场景2 完整修复工作流（诊断→修复→审批→PR，§3.5）
scripts/
├── watch_run.py            # run 逐阶段观测（节点状态/输出/token/审批/工具明细）
├── mock_mcp_server.py      # MCP 配置页测试用 mock server（mcp v1 FastMCP）
└── verify_sandbox.py       # 沙箱 K8s 端到端验证
docker/
├── sandbox/               # 沙箱镜像（stdlib-only，离线可建；WITH_JDK=1 加 Java）
└── Dockerfile.worker      # Worker 镜像（python:3.12-slim + 在线 pip 装 agentflow）
deploy/
└── worker-deployment.yaml # v5.3 §6.2 每租户 Worker Deployment（示例 team-alpha）
docs/
└── DEPLOYMENT_zh-CN.md    # v5.3：部署矩阵/Kafka ACL/每租户 Worker/本地 minikube 实操
tests/                 # 268 tests（DAG/幂等/Resume/审批/Worker/队列/多租户路由与数据面）
```

## M4 沙箱（独立执行 Pod）

```bash
# 1. 构建沙箱镜像（stdlib-only 离线可建；需要 Java 编译时加 --build-arg WITH_JDK=1）
docker build -t agentflow-sandbox:latest -f docker/sandbox/Dockerfile .
minikube image load agentflow-sandbox:latest

# 2. K8s 端到端验证（拉起沙箱 Pod → exec → 销毁）
./venv/bin/python scripts/verify_sandbox.py
```

- `sandbox/exec_service.py`：纯 stdlib http.server，零依赖；§10.2 限制（300s/1MB/10 并发/写白名单）
- `sandbox/orchestrator.py`：K8s 动态拉起/销毁沙箱 Pod（非特权 + drop ALL + cpu 2/mem 4Gi + /workspace 卷）
- `sandbox/action_executor.py`：§10.3 白名单动作（scale[0,10]/restart/patch_resources 范围/delete_temp 路径）
- `sandbox/policy.py`：§9.5 租户工具策略（deny 优先→allow→兜底 DENY）
- L2 工具（sandbox_run_python/shell/write_file）经 SandboxClient 进沙箱（§4.1 推理/执行分离）

## testbed 真实联调（走 MCP，v5.5 批3 起）

**数据查询全部经 MCP server**（进程内直连实现已删除）。需要两个进程：

```bash
# 0. 起 MCP 数据源 server（独立仓库 aiops-mcp-servers）
cd ~/accenture/workspace/aiops-mcp-servers/servers/aiops-datasource-mcp-server
cp .env.example .env
cd .. && uv run python -m aiops_datasource_mcp_server          # 监听 :8300

# 1. 部署 testbed（services + ES/Prometheus + configmaps + port-forward）
cd ../../agentflow-testbed && bash scripts/port-forward-all.sh

# 2. 注册 MCP server 并绑定到取数 agent
curl -X POST localhost:8000/mcp-servers -H 'Content-Type: application/json' \
  -d '{"name":"aiops-datasource","transport":"http",
       "config":{"url":"http://127.0.0.1:8300/mcp"}}'
# 用返回的 id 逐个绑定 triage / log-analyst / trace-analyst /
# metrics-analyst / infra-locator / root-cause：
curl -X PUT localhost:8000/agent-configs/metrics-analyst \
  -H 'Content-Type: application/json' -d '{"mcp_server_ids":["<id>"]}'
# ⚠️ 绑定后需**重启 worker** 才生效（worker 侧配置解析器为永久缓存）

# 3. 跑一次 run（时间窗必填），并用 watch_run.py 逐阶段观测
curl -X POST localhost:8000/run -H 'Content-Type: application/json' \
  -d '{"workflow_yaml":"<bug-fix-scenario2.yaml 内容>",
       "inputs":{"bug_report":{...},
                 "window_start":"2026-09-10T08:30:00",
                 "window_end":"2026-09-10T10:30:00"}}'
./venv/bin/python scripts/watch_run.py --recent --traces
```

> ⚠️ 每场景需**干净日志窗口**：连续跑两场景会互相污染（场景1 残留干扰场景2 定位）。
> 切换前清 ES：`curl -X DELETE :19200/app-logs`。

**时间窗由调用方下发**：`start_time`/`end_time` 是数据工具的必填参数，窗口来自事件源
（工单 `opened_at` / 告警触发时刻），算好后经 `inputs.window_start/window_end` 传入——
agent 不知道"当前时间"，不能让它在运行期猜（猜错会得到错误范围，且看不出异常）。

**故障 span 判定**住在 MCP server 侧：ES 按 traceId 重建调用链，优先「业务根因」而非
「feign 下游调用症状」。该启发式是**测试床特定经验**，非通用算法。

详见 `docs/design-v5.5.md`。

## 控制面 API（27 端点）

```
POST /run                          触发 run（JWT 模式 tenant 由 token 派生）
GET  /runs/{id}                    聚合详情（图/节点/tokens/cost/pending_approvals）
GET  /runs/{id}/traces             节点级 LLM 对话/工具调用明细
POST /runs/{id}/approve|reject     审批（CAS + 时间谓词；白名单校验；冲突 409）
POST /runs/{id}/pause|resume       暂停 / 断点续跑（queue 模式发布命令）
POST /runs/{id}/stop               停止（置 cancelled）
POST/GET/PUT/DELETE /workflows     流程 CRUD + /workflows/preview
POST/GET/PUT/DELETE /mcp-servers   MCP server 配置 + /test 连测 + /{id}/tools
POST/GET/PUT/DELETE /agent-configs Agent 配置（DB 驱动，热生效）
GET  /agents                       15-agent 编队视图
GET  /audit                        审计查询（JWT 模式 tenant 强制派生）
GET  /health                       存活检查
```

## 设计要点对照

- **DAG 语义**（§8.2）：`join: any|all` + 条件边 `when` + `SKIPPED` 级联传播；
  审批节点参与 skip（S-010b 实测）；`rejected-canceled`（审批超时）为正式终态，
  下游拒绝路径可求值。
- **审批**（§8.3）：CAS 更新 + **时间原子判定**（approve/reject 仅未超时可批、
  TIMED_OUT 仅超时后可置）+ 终态不可逆；审批挂起时 Worker 仅在 ready 集为空时释放（§8.6）。
- **幂等**（§8.4）：`execution_id` + `external_operation_id` 去重，副作用只发生一次；
  副作用 agent（committer/infra-remediator）自动带 `run_id:node_id` 确定性键，
  YAML 节点可声明 `idempotency_key`（`$.` 引用）做内容键（§8.4.3）。
- **版本冻结**（§8.5）：Run 创建时 snapshot（YAML hash 去重），Resume 用原版本。
- **Worker/双队列**（§6/§8.6）：`run_mode=queue` 时 API 只发布 run.trigger/run.command，
  `worker.Worker` 消费执行；审批完成 API 仅 CAS + 发 resume 命令（零进程内 executor
  依赖，多副本安全）；pause 为波间暂停（当前节点跑完即停）。
- **多租户**（v5.3 五原则 P1-P5）：数据面=租户自有 MCP（共享数据源默认下线）→
  队列=topic-per-tenant（broker ACL 边界）+ 每租户 Worker → K8s namespace 隔离 →
  per-tenant DB（TenantStoresRouter 路由，跨租户查询物理不可能）+ 管理库 →
  branch-per-tenant（管理库记 pinned_sha）。认证：JWT 派生 tenant（客户端提交忽略）
  → 跨租户 404 → 配额 429 / 审批人 default-deny 403。
- **工具治理**（§7/§10.4）：Tool Registry 定义 agent 可见性 / 超时 / 限流 / 结果上限。

## 环境与密钥

见 [.env.example](.env.example)。所需账号/Key 状态记录于项目根目录 `design-v5.2.md` §16
及会话记录。M0-M2 开发**无需外部数据源凭证**（mock 数据源即可跑通）。

## 本地基础设施（podman + minikube）

- podman-machine 已按 §16.2 审批调整至 **8C / 12G / 60G**（先 `podman machine start podman-machine-v5`）。
- **中间件**（`docker-compose up -d postgres kafka redis`，全部 `docker.io` 源）：
  - PostgreSQL `:5432`（§8.8 表）；Redis `:6379`；Kafka KRaft 单节点。
  - Kafka **双 listener**：`10.89.0.9:9092`（静态 IP，供 minikube K8s Pod）/ `localhost:19092`（宿主：
    须设 `AGENTFLOW_KAFKA_BOOTSTRAP=localhost:19092`）。
- K8s：minikube（M4 沙箱 / testbed / **每租户 Worker Deployment** 部署时使用）。
  `minikube start --driver=docker --force --cpus=6 --memory=9216 --container-runtime=containerd`
- **minikube ↔ compose 打通**：`podman network connect backend_default minikube`（kicbase 并入中间件网络，
  供沙箱/Worker Pod 经 hostNetwork 访问 PG/Kafka）。kafka 静态 IP 见 compose `networks.ipam`。
- **Worker Deployment 实操**（`agentflow-worker` 镜像 + `deploy/worker-deployment.yaml`）：
  `docker build -t agentflow-worker:local -f docker/Dockerfile.worker . && minikube image load agentflow-worker:local &&
  kubectl apply -f deploy/worker-deployment.yaml`；详见 `docs/DEPLOYMENT_zh-CN.md` §本地实操。
