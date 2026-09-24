# CLAUDE.md — agentflow 后端开发指南

AIOps Bug Fix 智能体平台后端。**本文件每个 session 全量加载，所以它只放"每次都要知道的"** ——
细节按触发条件拆到 `docs/constraints/`（见下方「约束索引」）。

## 开发命令

```bash
make install   # 创建 venv + 安装依赖
make doctor    # ⭐ 环境体检（换机器时先跑这个）；INSTALL=1 时把能装的装上
make test      # 跑 pytest（M0/M2 语义 + 幂等 + Resume）
make api       # 控制面 FastAPI（:8000/docs）
make lint      # ruff **+ §11 分层契约**（`.importlinter`，配置驱动，两条都过才算绿）
make codegraph # 代码索引：没建就建、有就增量同步（团队约定见 `docs/tooling/`）
make sync-workflows TENANT=<id>   # 把 seed 的 workflow 推到**已开通租户**（见 §6.0）
make sync-agents    TENANT=<id>   # 同上，推数据面（MCP server + agent 绑定）
```

> **查代码用 `codegraph`，别一上来就 grep**：`callers` / `callees` / `impact`
> 直接答"谁调用谁、改前波及什么"，`explore` 给 verbatim 原文（字面常量不丢）。
> 实测比 understand-anything 便宜两个数量级（0 token vs 824k）。
> 它与 grep 的实测分界、以及**纯 CLI 下会静默返回过期结果**这个坑，
> 见 [`docs/tooling/README.md`](docs/tooling/README.md)。

> **换一台机器、或者别人第一次接手：先 `make doctor`。** 本系统依赖一批**机器相关**的
> 外部件（postgres / kafka / 沙箱 / **gh CLI**），它们的共同点是**缺了不报错** ——
> 只在某条 run 跑到某一步时表现为"结果不对"。`doctor` 把这些问题摆在装环境的时候，
> 并给出可直接粘贴的修复命令（`--install` 能自动装 gh）。
> 判据只有一份：基础设施那几项复用 `tenantctl._env_preflight`，与 provision 时看到的是同一套。
>
> ⭐ **另查一条「工具版本基线」**（`toolchain.toml`）—— 与"在不在"正交。因为
> **版本漂移是静默的**：`ruff>=0.5` 这个无上界约束让一次 `pip install` 换掉了 ruff，
> `make lint` 凭空变红且**一直是红的**（实测）。"在不在"这类检查永远抓不到它。
> 改了工具版本，要么升工具、要么改 `toolchain.toml` —— 别让它悄悄漂着。

## 每个 task 的收尾（硬性流程）

**一个 task 做完，不要直接进入下一个。按顺序走完这三步再提交：**

1. **Clean code** —— 读一遍自己的 diff，问：
   - 命名/注释与**周围代码**一致吗？（本仓的注释解释"为什么"，不解释"是什么"）
   - 有没有重复实现？本仓已因跳层产生过两份 `_mark_cancelled`（见 §11），新增逻辑先找有没有现成的
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
>
> ⚠️ **验证必须交产物，不是自述**：写「命令 + 退出码 + 关键输出行」，不写"已验证"。
> 变异验证要附**变异真的生效**的证据（`grep -c` 归零 / diff），**不是"测试红了"** ——
> 本仓实测过"变异根本没植入、测试红了另有原因"的那次（见 `docs/retro/2026-09-22-retro.md` §1.1）。

## Git 约定

- **后端直提 `main`**（本仓不在 PR 流程里）；前端 `service-intelligence-platform-ui`
  提 `vue` 分支。**不开新的 feature 分支**。
- **一个 task 一个 commit**，不要把两件事混进一个 commit。
- 提交信息：首行 `<type>(<scope>): <做了什么>`（`fix` / `feat` / `docs` / `refactor`），
  正文写**为什么**（这是本仓最有价值的部分——多数缺陷的成因比结论更值得留），
  并写明**验证方式**（跑了哪些测试、有没有做变异验证）。
- **push 前先确认工作区干净、全量测试基线没退化**（当前基线见 `docs/TODO.md` §10）。
- ⚠️ **push 前再 `git fetch` 看一次**：本仓是多人并行直提 `main`，远端在你干活期间动过是常态。
  （实测过：push 被拒才发现落后，且有一次**方案的前提已经被同事的提交作废**。）

## 约束索引（**改代码前按触发条件读对应的那一份**）

> 这里只放**触发条件**与**一句话判据**；正文在 `docs/constraints/`。
> ⚠️ `§N` 是**稳定标识** —— README / 测试 / design / 源码注释里有 14 处按它引用，
> 拆文件时刻意不改编号，正是为了不让那些引用断掉。

| 触发 | 约束 | **一句话判据** | 正文 |
|---|---|---|---|
| 动依赖 / 升级 AgentScope | §1 版本锁定 | 升级前必须重跑 S-001/S-011 | [`01`](docs/constraints/01-agentscope-lock.md) |
| 动 StateStore/Queue/Lock | §2 基础设施可插拔 | 只经 `base.py` 接口访问，配置驱动切换 | [`02`](docs/constraints/02-infra-pluggable.md) |
| **改 `core/dag.py` / 写 workflow YAML / 加节点 kind** | §3 DAG 语义 | `join` 默认 `any` ⇒ **多入边几乎总要显式 `join: all`**；`kind: halt` 一旦执行其余全 SKIPPED、`kind: closed` **不**跳（别并进 `halt_triggered()`）；`VERDICT_FIELDS`「**投不出去必须是红的**」、`ARTIFACT_FIELDS`「**声称改了≠真改了**」 | [`03`](docs/constraints/03-dag-semantics.md) |
| 动审批 / 调人工决策点 | §4 审批 CAS | **严禁绕过 CAS 改终态**；每个审批节点**必须显式**声明 `on_reject`；判据看**状态**不看**动作** | [`04`](docs/constraints/04-approval.md) |
| 加副作用节点 / 动 checkpoint | §5 副作用幂等 | 新副作用节点**必须进清单或声明幂等键**；`update_node_status` 必须同步写 `cp` 列 | [`05`](docs/constraints/05-idempotency.md) |
| **改 workflow（含 seed）/ 动 resume / 动队列模式** | §6 真源 + 冻结 | **真源是数据库**，改 seed 对已开通租户无效 ⇒ `make sync-workflows`；**冻结数据一律 `strict=False`** | [`06`](docs/constraints/06-workflow-truth.md) |
| 动存储路由 / 加控制面表 / 租户隔离 | §7 多租户 | 新读路径**必须**经 `service.store_for()` / router；**新增控制面表务必加进 `TenantStores`** | [`07`](docs/constraints/07-multitenancy.md) |
| 动 workspace / 工作区 git | §8 Git 版本冻结 | 工作区 HEAD 必须 == `base_sha`，漂移报 `FrozenVersionMismatch` | [`08`](docs/constraints/08-workspace-git.md) |
| 动 agent 工具 / param 解析 | §9 工具权限 | 没 allow 规则时 DONT_ASK 下工具**全部 DENY**；`output` 是标准访问器、**不能当字段遍历** | [`09`](docs/constraints/09-tools-permissions.md) |
| **动沙箱 / 写文件 / 跑测试 / 排查"修复不落盘"** | §9.6 沙箱 | **谁持有密钥，谁不执行不可信代码** | [`09.6`](docs/constraints/09.6-sandbox.md) |
| 动 committer / 建 PR / 配 gh | §9.7 PR 后端 | 缺 gh ⇒ `commit` 失败 ⇒ **下游 `ticket-done` 根本不执行**，闭环无回音 | [`09.7`](docs/constraints/09.7-pr-gh.md) |
| 动取数 / MCP 注册绑定 / CMDB | §10 数据源=MCP | 查询**必须带时间区间与目标**；metric 是领域语义，**不传 PromQL** | [`10`](docs/constraints/10-datasource-mcp.md) |
| **加新模块 / 移动文件 / 改 import** | §11 分层 | **API → Service → Repository 单向**；判层看 `import`，**不看注释** | [`11`](docs/constraints/11-layering.md) |

## 经验索引（团队共享的教训）

> **经验此前只沉在个人机器上**——Claude Code 的跨 session 记忆是 `~/.claude/…`，**队友拿到 0 条**。
> 这个索引就是那条通道。全文：[`docs/lessons/INDEX.md`](docs/lessons/INDEX.md)。
>
> ⚠️ **它不是"更多文档"，是"待机器化的清单"**：收录的 16 条里 **15 条可以写成"如果…就红"的检查** ——
> 检查一旦存在，这条经验就**不需要被记住**了。每条都标了去向（✅已机器化 / 🔴待机器化 / 📄只能文档）。

最常踩的三条（全文与其余 13 条见上表）：

- **"看着全绿实则为空"**（本仓最高频）：改完接线类代码问三句 —— ① 部署形态下真走到这条路径吗？
  ② 下游按图里写的方式读，读到的是值还是 `None`？③ **这个能力的消费方是谁**（`grep 类名` 只命中 docstring = 零消费方）。
- **改了 prompt 或 schema 必须重启 API 与 worker** —— 否则静默跑旧 prompt，表现为"这个方案不工作"。
- **跨仓传标识先确认词义**：`ticket_id` 在 MCP 那边装 `INC-…`，在 APM 那边指 agentflow 内部 id
  （`INC-…` 在 APM 叫 `ticket_number`）—— 按 `ticket_id` 查**永远查不到**。

## 结构速览

```
core/        Workflow 模型 + DAG 语义 + 版本冻结（M0）
statestore/  State Model（memory/sqlite/postgres）+ router.py（租户库路由）
executor/    并发 DAGExecutor + 幂等 + Retry + Resume（M2）
agents/      16-agent 编队（9 诊断 + 7 修复）+ AgentScope 适配 + 工具治理（M1 骨架）
             └ scopes.py  build_permission_context（§9 DONT_ASK+allow）
datasource/  ⚠️ 架构例外：Prometheus 直连（仅服务遗留前端 Smart Inspection，见 §7 的裁决边界）
workspace/   WorkspaceManager（M3）；CMDB 已迁 MCP
sandbox/     M4：exec 服务(纯 stdlib) + SandboxClient + Orchestrator + ActionExecutor + ToolPolicy
approval/    M5：审批超时 Sweeper + 通知
audit/       M5：审计日志
queue/ lock/ 可插拔队列/锁（memory + kafka/redis 生产适配器）
service.py   RunService：create / approve / resume 编排          ← Service 层（§11）
api/         ⚠️ 按 §11 **只应放 API 层**，当前**混入了一层 Repository**：
             │  app.py / auth.py  ← API 层；五个 *_store.py ← **实为 Repository，放错了层**
             └ 它们被 statestore/router.py 与 worker.py 依赖 —— **反向依赖**（整改见 TODO §14）
tenantctl.py 租户生命周期 CLI（**在顶层**，不在 api/ 下）
scripts/     watch_run.py + mock_mcp_server.py + verify_sandbox.py
             + push_seed_workflows.py / push_seed_agents.py（推 seed 到已开通租户，§6.0）
docker/sandbox/  沙箱镜像（stdlib-only 离线可建）
docs/constraints/  按触发条件拆出的约束正文（见上方索引）
docs/retro/        回顾：我们做事的方式哪里在失效
```

## 文档地图（**哪份给谁看** —— 写之前先问"谁在什么时刻读它"）

| 文档 | 受众 | 什么时候读 |
|---|---|---|
| `CLAUDE.md`（本文件） | **LLM 常驻** + 新人 | 每个 session / 接手时 |
| `docs/constraints/*` | **LLM 按需** | 按上面索引的**触发条件** |
| `docs/TODO.md` | 人（排期）+ LLM（查"这是不是已知问题"） | — |
| `docs/design-v5.8.md` | 人（架构决策） | 设计变更时 |
| `docs/retro/` | **人** | 里程碑结束 |
| 提交信息 | **LLM 按需**（`git log` 查为什么）+ 人 | 溯源 |
| `docs/lessons/*` | **LLM 按需**（团队共享的教训） | 按索引的触发条件 |
| `docs/tooling/*` | **人**（新队友上手开发工具）+ LLM 按需 | 要用/要改某个开发工具时；`toolchain.toml` 是它机器可查的那一半 |

> ⚠️ **本表本身就是一条约束**：写新文档前先回答「**谁在什么时刻会读它**」——
> **答不出来的不要写**。本仓有过 134 个既没索引也没入口的 changelog，
> 人和 LLM 都不会读到它们（见 `docs/retro/2026-09-22-retro.md` §7.1）。

## 测试

- `tests/test_workflow.py`：加载/冻结/静态校验 · `test_executor.py`：join/skip/审批/失败 abort
- `tests/test_resume.py`：断点续跑 + RunService 端到端 · `test_idempotency.py`：幂等键 / retry / 负证据
- `tests/test_workspace.py`：base_sha 冻结 / 分支隔离 / 无 pull
- `tests/test_app_indicators*.py`：PromQL 构造（`container!=""`）/ 不鉴权回归 / 失败仍 200
- `tests/test_service_meta.py`：Deployment label → owner/type/agentName；**K8s 读失败不上升**
- demo 用脚本化 runner（无真实 LLM）；真实模型见 `agents/scopes.py:build_model`

## 里程碑

M0–M5 ✅ → M6 🟡（适配器可用，真实 broker/DB 专项待生产）→ M7 🟡（诊断真实，解决侧部分 mock）。
多租户 v5.3 批 A/B/C ✅ · 工单回传闭环 ✅（2026-09-21，三仓链路端到端实测，
设计见 `docs/design-v5.8.md` §13）。
待办（**全量见 `docs/TODO.md`**，此处不复制）：MCP 凭证加密、JWT JWKS、沙箱 API 认证、
真实 Kafka/PG 故障恢复专项。
