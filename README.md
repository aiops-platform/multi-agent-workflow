# agentflow — AI 运维 Bug Fix 智能体平台后端

基于 `design-v5.2.md` 的 **M0-M2 脚手架**（适配层 / 编排引擎 / DAG 语义）。

## 里程碑状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M0** | Workflow 版本冻结 + DAG 语义（join/skip）+ State Model | ✅ 已实现（见 `agentflow/core` + `statestore`） |
| **M1** | AgentScope 适配层（2.0.3 锁定）+ 15-agent 编队 + 工具治理 | 🟡 骨架就绪（`agents/`），mock 数据源可跑 |
| **M2** | DAG Executor + Node Attempt + Retry + Resume | ✅ 已实现（见 `executor/`），含复杂拓扑补测 |
| **M3** | Workspace（Git base_sha 冻结 + 分支隔离）+ CMDB 驱动 | ✅ 已实现（见 `workspace/`），37 tests |
| **M4** | Sandbox（独立 Pod + 安全基线）+ Tool Policy + Action Executor | ✅ 已实现（见 `sandbox/`），K8s 端到端验证通过，52 tests |
| **M5** | Approval（CAS + Timeout + Sweeper）+ Notification + Audit Log | ✅ 已实现（见 `approval/` + `audit/`），60 tests |
| **M6** | 生产适配器（Kafka/PostgreSQL/Redis）+ 故障恢复专项测试 | ✅ 已实现（见 `queue/statestore/lock`），71 tests |

> AgentScope 版本**锁定 2.0.3**（design §5）。升级前必须重跑 S-001/S-011。

## 快速开始

```bash
# 1. 安装
make install                     # 或 ./venv/bin/pip install -e ".[dev]"

# 2. 配置（模型 + 基础设施后端）
cp .env.example .env            # 填 DEEPSEEK_API_KEY（design §16.3 模型 deepseek-v4-flash）

# 3. 跑测试（M0/M2 语义 + 幂等 + Resume）
make test

# 4. 跑脚本化 demo（create_run → 审批 → done）
make demo

# 5. 控制面 API
make api                        # http://localhost:8000/docs
```

## 目录结构

```
agentflow/
├── config.py          # LLM + StateStore/Queue/Lock 后端切换（配置驱动）
├── core/              # M0：Workflow 模型 + DAG 语义（join/skip）+ 版本冻结
├── statestore/        # M0：State Model（InMemory / SQLite，表结构对齐 §8.8）
├── queue/ lock/       # M0/M6：可插拔队列/锁（memory + kafka/redis 生产适配器）
├── executor/          # M2：并发 DAG Executor + 幂等 + Retry + Resume
├── agents/            # M1：15-agent 编队 + AgentScope 适配 + 工具治理 + 权限上下文
│   └── datasources.py # 真实数据源适配（ES/Prometheus/kubectl，testbed 联调）
├── workspace/         # M3：WorkspaceManager（base_sha 冻结/分支隔离/无 git_pull）+ CMDB
├── sandbox/           # M4：exec 服务(纯 stdlib) + SandboxClient + SandboxOrchestrator + ActionExecutor + ToolPolicy
├── approval/          # M5：审批超时 Sweeper（§8.9）+ 通知
├── audit/             # M5：审计日志（§9.5 字段 + 输入脱敏）
├── statestore/postgres.py  # M6：PostgreSQL 生产适配器（§8.8 完整 schema）
├── api/               # 控制面 FastAPI（sweeper 后台任务 + GET /audit）
└── service.py         # RunService：create / approve / resume 编排
workflows/
└── bug-fix-pipeline.yaml   # design §8.1 完整示例
scripts/
├── diagnose_scenario1.py   # 场景1 真实联调：DeepSeek + 真实数据源诊断链
└── diagnose_scenario2.py   # 场景2 真实联调
docker/sandbox/             # 沙箱镜像（stdlib-only，离线可建；WITH_JDK=1 加 Java）
tests/                 # M0-M4 语义测试（52 tests）
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

## 设计要点对照

- **DAG 语义**（§8.2）：`join: any|all` + 条件边 `when` + `SKIPPED` 级联传播；
  审批节点参与 skip（S-010b 实测）。
- **审批**（§8.3）：CAS 更新 + 终态不可逆；审批挂起时 Worker 仅在 ready 集为空时释放（§8.6）。
- **幂等**（§8.4）：`execution_id` + `external_operation_id` 去重，副作用只发生一次。
- **版本冻结**（§8.5）：Run 创建时 snapshot（YAML hash 去重），Resume 用原版本。
- **多租户**（§9）：所有表带 `tenant_id` 分区键。
- **工具治理**（§7/§10.4）：Tool Registry 定义 agent 可见性 / 超时 / 限流 / 结果上限。

## 环境与密钥

见 [.env.example](.env.example)。所需账号/Key 状态记录于项目根目录 `design-v5.2.md` §16
及会话记录。M0-M2 开发**无需外部数据源凭证**（mock 数据源即可跑通）。

## 本地基础设施（podman + minikube）

- podman-machine 已按 §16.2 审批调整至 **8C / 12G / 60G**。
- K8s：minikube（M4 沙箱 / testbed 部署时使用）。
