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
   审批节点参与 skip。改语义必须同步 `tests/test_executor.py` 的 S-010b 场景。
4. **审批 CAS + 终态不可逆**（`statestore/base.py:cas_update_approval`）。严禁绕过 CAS 改终态。
5. **副作用幂等**（`executor/idempotency.py`）：`execution_id` 唯一 + `external_operation_id`
   复用。新增副作用节点必须定义幂等键（§8.4.3）。
6. **版本冻结**（`core/workflow.py`）：Run 用 `workflow_hash` 复用 snapshot，Resume 只读原 snapshot。
7. **多租户**：所有表带 `tenant_id`；租户身份由 JWT 派生（§9.1），代码里禁止以客户端提交的
   tenant 为授权依据（M5 接入 Gateway 前本地联调可显式传参）。
8. **Git 版本冻结**（§4.6/§8.7）：`workspace/manager.py` 明确不提供 git_pull；Run 期间工作区
   HEAD 必须 == base_sha，漂移报 `FrozenVersionMismatch`。每个 Run 用 `aiops/RUN_{run_id}` 分支隔离。
9. **工具权限**（§9.5）：`build_agent` 默认 DONT_ASK + agent 注册工具的 allow 规则。
   没有 allow 规则时 DONT_ASK 下工具全部 DENY（联调踩过：agent 只能靠提示词推理）。
   `build_permission_context` 生成上下文；租户 deny 规则 M5 接入。
10. **真实数据源**（testbed 联调）：`datasources.py` 的 adapter 与 mock 工具签名一致
    （SCENARIOS §5.2），数据源切换只换 adapter。ES index `app-logs`（app.* 字段）、
    Prometheus cAdvisor（`container_*`）、kubectl namespace `order`。联调脚本
    `scripts/diagnose_scenario1.py`（需 `source ../spike/.env` 供 DEEPSEEK_API_KEY）。

## 结构速览

```
core/        Workflow 模型 + DAG 语义 + 版本冻结（M0）
statestore/  State Model：InMemory / SQLite（M0）
executor/    并发 DAGExecutor + 幂等 + Retry + Resume（M2）
agents/      15-agent 编队 + AgentScope 适配 + 工具治理（M1 骨架）
             ├ datasources.py  真实数据源适配（ES/Prometheus/kubectl，testbed）
             └ scopes.py       build_permission_context（§9.5 DONT_ASK+allow）
workspace/   WorkspaceManager + CMDB（M3）
queue/ lock/ 可插拔队列/锁（memory + M6 stub）
service.py   RunService：create / approve / resume 编排
api/         控制面 FastAPI
workflows/   bug-fix-pipeline.yaml（design §8.1）
scripts/     diagnose_scenario1.py（真实联调）
```

## 测试

- `tests/test_workflow.py`：加载/冻结/静态校验（§8.2.3/§8.5）
- `tests/test_executor.py`：join/skip/审批 CAS/skip 级联/失败 abort
- `tests/test_resume.py`：SQLite 断点续跑 + RunService 端到端
- `tests/test_idempotency.py`：external_operation_id 复用 / retry / 负证据
- `tests/test_workspace.py`：base_sha 冻结 / 分支隔离 / 幂等 / 无 pull（file:// 本地源）
- demo 用脚本化 runner（无真实 LLM）；真实模型见 `agents/scopes.py:build_model`

## 里程碑

M0 ✅ → M1 🟡（mock 可跑）→ M2 ✅ → M3-M6 ⏳。下一步（M3）：
WorkspaceManager（Git base_sha 冻结）+ testbed 联调。
