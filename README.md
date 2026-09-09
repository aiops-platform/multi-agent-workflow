# agentflow — AI 运维 Bug Fix 智能体平台后端

基于 `design-v5.2.md` 的 AIOps Bug Fix 智能体平台：DAG 编排引擎 + 15-agent 编队 +
审批工作流 + Worker/双队列执行 + JWT 多租户（244 tests）。

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

# 3. 跑测试（244 tests：DAG 语义 + 幂等 + Resume + Worker/队列 + JWT 多租户）
make test

# 4. 跑脚本化 demo（create_run → 审批 → done）
make demo

# 5. 控制面 API
make api                        # http://localhost:8000/docs
```

## 执行模式与多租户

```bash
# ── 执行模式（design §6/§8.6）──
AGENTFLOW_RUN_MODE=inline   # 默认：API 进程内直接执行 DAG（本地 MVP）
AGENTFLOW_RUN_MODE=queue    # API 只发布 run.trigger / run.command，Worker 消费：
                            #   queue=memory → API 启动时自动拉起进程内 Worker
                            #   queue=kafka  → 单独运行：python -m agentflow.worker

# ── 多租户认证（design §9.1）──
AGENTFLOW_JWT_SECRET=...    # 非空 = 强制 Bearer JWT，tenant 由 claim（tenant_id/org_id）
                            #   派生，客户端提交的 tenant 一律忽略
                            # 为空 = dev 模式：回退 X-Tenant-ID 头 / 请求体传参（仅联调）

# ── 租户配置（design §9.3：配额 + 审批人白名单）──
AGENTFLOW_TENANTS_FILE=tenants.yaml
```

```yaml
# tenants.yaml 示例
default: { max_concurrent_runs: 10 }
tenants:
  team-alpha:
    max_concurrent_runs: 5
    approvers:
      approve-changes: ["alice@company.com"]   # 白名单外审批 → 403；超限 → 429
```

## 目录结构

```
agentflow/
├── config.py          # LLM + StateStore/Queue/Lock + run_mode + JWT 后端切换（配置驱动）
├── core/              # M0：Workflow 模型 + DAG 语义（join/skip）+ 版本冻结
├── statestore/        # M0/M6：State Model（InMemory / SQLite / PostgreSQL，表结构对齐 §8.8）
├── queue/ lock/       # M0/M6：可插拔队列/锁（memory + kafka/redis 生产适配器）
├── executor/          # M2：并发 DAG Executor + 幂等 + Retry + Resume + 波间暂停
├── worker.py          # §6/§8.6：Worker 池（消费 run.trigger / run.command；python -m agentflow.worker）
├── agents/            # M1：15-agent 编队 + AgentScope 适配 + 工具治理 + 权限上下文
│   ├── agent_config.py    # DB 驱动 agent 配置解析（DB 覆盖 + 内置回退合并）
│   ├── mcp_manager.py     # MCP server 连接管理（stdio/http + 热刷新 + 按 agent 绑定）
│   ├── runner.py          # AgentNodeRunner：真实 LLM 接入 DAGExecutor
│   ├── transcript.py      # 节点级 LLM 对话/工具调用明细采集 → node_traces
│   └── datasources.py     # 真实数据源适配（ES/Prometheus/kubectl，testbed 联调）
├── workspace/         # M3：WorkspaceManager（base_sha 冻结/分支隔离/无 git_pull）+ CMDB
├── sandbox/           # M4：exec 服务(纯 stdlib) + SandboxClient + SandboxOrchestrator + ActionExecutor + ToolPolicy
├── approval/          # M5：审批超时 Sweeper（§8.9）+ 通知
├── audit/             # M5：审计日志（§9.5 字段 + 输入脱敏）
├── tenants.py         # §9.3：租户配置（max_concurrent_runs 配额 + approvers 审批人白名单）
├── api/               # 控制面 FastAPI（27 端点 + JWT 租户派生 + sweeper 后台任务）
│   └── auth.py            # §9.1：JWT → 派生 tenant_id（get_tenant_context 依赖）
└── service.py         # RunService：create / approve / resume / pause / stop（inline|queue 双模式）
workflows/
├── bug-fix-pipeline.yaml   # design §8.1 完整示例
└── bug-fix-scenario2.yaml  # 场景2 完整修复工作流（诊断→修复→审批→PR，§3.5）
scripts/
├── diagnose_scenario1.py   # 场景1 真实联调：DeepSeek + 真实数据源诊断链
├── diagnose_scenario2.py   # 场景2 真实联调
└── run_fix_loop.py         # 场景2 修复闭环 E2E（真实工作区 git 修复 + 审批 + PR）
docker/sandbox/             # 沙箱镜像（stdlib-only，离线可建；WITH_JDK=1 加 Java）
tests/                 # 244 tests（DAG/幂等/Resume/审批/Worker/队列/JWT 多租户）
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

## testbed 真实联调（场景1 + 场景2 已验证 ✅）

```bash
# 1. 部署 testbed（services + ES/Prometheus + configmaps + port-forward）
cd ../../agentflow-testbed && bash scripts/port-forward-all.sh

# 2. 场景1：注入故障（磁盘 + CPU 打满）→ 诊断 → 恢复
bash fault-inject/scenario1.sh
cd ../backend && source ../spike/.env && ./venv/bin/python scripts/diagnose_scenario1.py
# → root_cause_type: infra_issue（磁盘 EmptyDir 写满），命中期望
cd ../../agentflow-testbed && bash fault-inject/scenario1-recover.sh

# 3. 场景2：注入故障（warranty fin 缺参 + 吞异常）→ 诊断 → 恢复
bash fault-inject/scenario2.sh
curl -s --max-time 8 -X POST "http://localhost:18080/checkout?orderId=ORD20260819001"   # 触发（挂起）
cd ../backend && source ../spike/.env && ./venv/bin/python scripts/diagnose_scenario2.py
# → root_cause_type: code_bug（warranty-service fin 缺参），命中期望
cd ../../agentflow-testbed && bash fault-inject/scenario2-recover.sh
```

> ⚠️ 每场景需**干净日志窗口**：连续跑两个场景会互相污染（场景1 残留干扰场景2 定位）。
> 切换前清 ES：`curl -X DELETE :19200/app-logs`。

数据源与工具签名一致（SCENARIOS §5.2），mock/真实切换只换 adapter，agent 定义不变。
`get_trace`：ES 按 traceId 重建调用链并判定故障 span（真实 testbed 的 traceId 未跨服务共享，
无 traceId 时回退最近时间窗；故障 span 优先「业务根因」而非「feign 下游调用症状」）。

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
- **多租户**（§9）：JWT 派生 tenant（客户端提交忽略）→ run 数据跨租户 404 →
  配额 429 / 审批人白名单 403；所有表带 `tenant_id` 分区键。
- **工具治理**（§7/§10.4）：Tool Registry 定义 agent 可见性 / 超时 / 限流 / 结果上限。

## 环境与密钥

见 [.env.example](.env.example)。所需账号/Key 状态记录于项目根目录 `design-v5.2.md` §16
及会话记录。M0-M2 开发**无需外部数据源凭证**（mock 数据源即可跑通）。

## 本地基础设施（podman + minikube）

- podman-machine 已按 §16.2 审批调整至 **8C / 12G / 60G**。
- K8s：minikube（M4 沙箱 / testbed 部署时使用）。
