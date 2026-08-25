# agentflow — AI 运维 Bug Fix 智能体平台后端

基于 `design-v5.2.md` 的 **M0-M2 脚手架**（适配层 / 编排引擎 / DAG 语义）。

## 里程碑状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M0** | Workflow 版本冻结 + DAG 语义（join/skip）+ State Model | ✅ 已实现（见 `agentflow/core` + `statestore`） |
| **M1** | AgentScope 适配层（2.0.3 锁定）+ 15-agent 编队 + 工具治理 | 🟡 骨架就绪（`agents/`），mock 数据源可跑 |
| **M2** | DAG Executor + Node Attempt + Retry + Resume | ✅ 已实现（见 `executor/`），含复杂拓扑补测 |
| M3-M6 | Workspace/Sandbox/Approval/生产适配器 | ⏳ 后续里程碑 |

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
├── queue/ lock/       # M0：可插拔队列/锁（memory + M6 stub）
├── executor/          # M2：并发 DAG Executor + 幂等 + Retry + Resume
├── agents/            # M1：15-agent 编队 + AgentScope 适配 + 工具治理
├── api/               # 控制面 FastAPI（M5 前最小形态）
└── service.py         # RunService：create / approve / resume 编排
workflows/
└── bug-fix-pipeline.yaml   # design §8.1 完整示例
tests/                 # M0/M2 语义测试
```

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
