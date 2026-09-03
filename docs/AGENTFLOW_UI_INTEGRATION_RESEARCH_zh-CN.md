# agentflow-ui 前端流程配置 与 multi-agent-workflow 后端对齐调研

> 调研日期：2026-09-01
> 调研范围：上级目录 `acc-aiops-platform-zjb/` 下三个仓库
> - `agentflow-ui/`（Vue 3 前端，含「流程配置」功能）
> - `agentflow/`（前端当前调用的后端服务）
> - `multi-agent-workflow/`（本仓库，本文档所在项目）
>
> **更新（v1.10.0，2026-09-01）**：§6 差距 2-8 与 §7 方案 A 已在
> 「SIP Bug Solve 页面 + multi-agent-workflow 后端 UI 兼容层」中落地实现。

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 前端「流程配置」功能调的是什么接口？ | 调 `agentflow` 后端的 **workflow CRUD**（`POST/GET/PUT/DELETE /workflows`）+ 预览时把 YAML 贴给前端本地解析，不调后端 |
| `multi-agent-workflow` 有没有后端能直接被前端调用？ | **有**。控制面 FastAPI（`agentflow/api/app.py`）已加 UI 兼容层：`POST /run` 异步化（`{workflow_id, ticket}`）、聚合 `GET /runs/{id}`、`approve/reject/stop`、`/workflows` CRUD（v1.9.0），可被 agentflow-ui / SIP Bug Solve 页直接调用 |
| 差距数量 | 调研时 7 个功能性差距 + 1 个字段级差距；**已全部落地解决**（详见 §6 / §7） |

---

## 2. agentflow-ui 前端「流程配置」功能拆解

前端目录：`agentflow-ui/src/`，Vue 3 + Vite + VueFlow（`@vue-flow/core`）。

### 2.1 页面结构（App.vue）

三个 Tab，由 `tab` 状态切换：

| Tab | 组件 | 功能 |
|---|---|---|
| **流程配置** | `WorkflowConfig.vue` | 粘贴 workflow YAML → 本地预览节点图 → 保存/编辑/删除/复用已存流程 |
| **agentflow** | `BugSolve.vue` | 选已存流程 → 贴 Ticket JSON → 开始 run → 轮询状态 → 审批/驳回 → 点节点看 prompt/output |
| **Agents** | `AgentsList.vue` | 展示已注册 agents（name / description / tools） |

### 2.2 「流程配置」页面（WorkflowConfig.vue）交互流程

1. 用户在左侧 panel **粘贴 workflow YAML**（textarea）。
2. 点「预览节点图」→ **前端本地** `js-yaml` 解析 YAML → 用拓扑分层算法画 VueFlow 图（`layout()` 函数，**不调用后端**）。
3. 点「保存流程」→ `POST /workflows {name, yaml}`；「更新流程」→ `PUT /workflows/{id}`。
4. 左侧「已保存流程」列表 → `GET /workflows`；「编辑」→ `GET /workflows/{id}`；「删」→ `DELETE /workflows/{id}`。
5. 点「去解决」→ 把 `id` 传给 BugSolve 页。

**关键点**：流程配置页的画图是前端本地渲染，后端只负责 **保存 / 校验 YAML**（`POST /workflows` 时后端 `parse_yaml_str` 校验合法性）与 **返回 graph 结构**（`GET /workflows/{id}` 的 `graph` 字段，供 BugSolve 页画运行图）。

### 2.3 前端调用的全部接口（api.js）

```js
// workflow CRUD
saveWorkflow:   POST   /workflows            body {name, yaml}
updateWorkflow: PUT    /workflows/{id}       body {name, yaml}
listWorkflows:  GET    /workflows
getWorkflow:    GET    /workflows/{id}       → {id, name, yaml, graph}
deleteWorkflow: DELETE /workflows/{id}
// run
startRun:       POST   /run                  body {workflow_id, ticket}
getRun:         GET    /runs/{run_id}
stopRun:        POST   /runs/{run_id}/stop
approveNode:    POST   /runs/{run_id}/approve body {node_id}
rejectNode:     POST   /runs/{run_id}/reject  body {node_id}
listAgents:     GET    /agents
```

### 2.4 前端依赖的响应结构（BugSolve.vue 轮询逻辑）

前端 `poll()` 每 2s 调 `GET /runs/{id}`，依赖以下字段：

```jsonc
{
  "run_id": "...",
  "status": "success | failed | cancelled | ...",
  "total_tokens": 123,
  "total_cost": 0.0123,
  "nodes": {
    "triage": { "status": "done", "tokens": 100, "cost": 0.001, "output": {...}, "prompt": "..." },
    "...": "..."
  },
  "pending_approvals": [
    { "node_id": "approve-changes", "trigger": "high-risk", "upstream": { "fix": {...} } }
  ]
}
```

- 节点状态值前端用：`done / running / pending / failed / cancelled / rejected-canceled / skipped`。
- 审批卡片展示 `node_id` + `trigger` + `upstream`（上游输出）。
- 点节点后展示该节点 `output` / `prompt` 与 `tokens / cost`。

### 2.5 前端代理配置（vite.config.js）

```js
proxy: { '/workflows': 'http://localhost:8000', '/agents': 'http://localhost:8000',
         '/run': 'http://localhost:8000', '/runs': 'http://localhost:8000',
         '/health': 'http://localhost:8000' }
```

前端 dev 直连 `localhost:8000`，即 `agentflow` 后端的 uvicorn 端口。

---

## 3. agentflow 后端服务（前端当前调用方）

目录：`agentflow/agentflow/server.py`（FastAPI 单体）。

### 3.1 端点清单（与前端 api.js 一一对应）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/workflows` | `{name, yaml}` | `{id, name, graph}`（保存时校验 YAML） |
| GET | `/workflows` | — | `[{id, name, created_at}]` |
| GET | `/workflows/{id}` | — | `{id, name, yaml, graph}` |
| PUT | `/workflows/{id}` | `{name, yaml}` | `{id, name, graph}` |
| DELETE | `/workflows/{id}` | — | `{ok: true}` |
| POST | `/run` | `{workflow_id \| yaml \| workflow, ticket, run_id?, resume?}` | `{run_id, status: "started"}`（**异步**） |
| GET | `/runs/{run_id}` | — | 聚合节点状态 + token/cost + pending（见 §2.4） |
| POST | `/runs/{run_id}/approve` | `{node_id}` | `{ok, run_id, node_id, approved}` |
| POST | `/runs/{run_id}/reject` | `{node_id}` | `{ok, run_id, node_id, approved: false}` |
| POST | `/runs/{run_id}/stop` | — | `{ok, run_id}` |
| GET | `/agents` | — | `[{name, description, tools}]` |
| GET | `/health` | — | `{status: "ok"}` |

### 3.2 关键实现

- **WorkflowStore**：SQLite `state.db` 建 `workflows` 表（`id/name/yaml/created_at`），`id` 为 `uuid4().hex[:12]`。
- **graph 提取**（`_workflow_graph`）：从 `WorkflowDef`（pydantic，`workflow/schema.py`）提取
  `{name, nodes: [{id, agent}], edges: [{from, to, when}]}`，供前端画图。
- **异步执行**（`POST /run`）：`OpenCodeAdapter`（opencode 运行时）→ `DAGExecutor`，`asyncio.create_task` 后台跑，立即返回 `run_id`。
- **审批**：`ApprovalManager`（`engine/approval.py`）按 run 存 `_approval_managers`，approve/reject/stop 直接操作。
- **/run 兼容三种入参**：`workflow_id`（DB 里查）> `yaml`（内联文本）> `workflow`（文件路径）。

### 3.3 workflow YAML 格式（agentflow 后端）

`workflow/schema.py` 的 `WorkflowDef`：

```yaml
name: bug-fix-pipeline
inputs:
  repo:       { type: string, required: true }
  bug_report: { type: object, required: true }
nodes:
  triage:  { agent: triage,           params: { bug: "$.inputs.bug_report" } }
  fix:     { agent: fix-implementer,  params: { plan: "$.nodes.plan.output" }, approve: high-risk }  # ← 审批是节点上的触发字段
  commit:  { agent: committer,        params: {...}, approve: write }
edges:
  - { from: triage, to: logs }
  - { from: test, to: review, when: "$.nodes.test.output.passed == true" }
```

- 节点字段：`agent / kind(agent|approval) / params / approve / retry / timeout / on_schema_error / on_failure / idempotency_key / input_view`。
- **审批模型**：agent 节点声明 `approve: high-risk|write`，执行时触发 pending 审批（非独立审批节点）。
- 参数引用约定 `$.nodes.<id>.output.*`，声明即产生隐式依赖边。

---

## 4. multi-agent-workflow 后端现状（本仓库）

目录：`agentflow/api/app.py`（控制面 FastAPI）+ `agentflow/service.py`（RunService）。

### 4.1 端点清单

| 方法 | 路径 | 请求 | 响应 | 前端能否用？ |
|---|---|---|---|---|
| POST | `/run` | `{workflow_id, ticket}`（兼容 `{workflow_yaml, inputs, tenant_id}`） | `{run_id, status:"started"}`（**异步**） | ✅ 已兼容（v1.10.0） |
| GET | `/runs/{run_id}` | — | **聚合**：graph + nodes + token/cost + pending_approvals | ✅ 已兼容（v1.10.0） |
| POST | `/runs/{run_id}/approve` | body `{node_id}`（兼容 query `node_id`） | `{approval, run_status, ...}` | ✅ 已兼容（v1.10.0） |
| POST | `/runs/{run_id}/reject` | `{node_id, by, comment}` | `{approval, run_status, ...}` | ✅ 已新增（v1.10.0） |
| POST | `/runs/{run_id}/stop` | — | `{ok, run_id}` | ✅ 已新增（v1.10.0） |
| GET | `/audit` | — | 审计日志列表 | 前端不用 |
| GET | `/agents` | — | `[{name, description, tools, stage}]` | ✅ 可兼容 |
| GET | `/health` | — | `{status, service}` | ✅ 可兼容 |

### 4.2 执行模型（v1.10.0 起为异步）

`POST /run` 经 `RunService.start_run`（service.py:54）**异步执行**：

```
start_run → save_snapshot（workflow_hash 冻结）→ create_run 行 → DAGExecutor 入 _executors
          → asyncio.create_task(_run_background) → 立即返回 {run_id, status:"started"}
_run_background → ex.run() 后台执行 → 终态/失败/取消 → update_run 落库
```

前端轮询 `GET /runs/{id}` 取进度/结果。多 agent 真实 LLM 执行可跑很久，异步模型让前端请求立即返回、可展示运行进度。

> 注意：`RunService.create_run`（同步阻塞）仍保留，供 `scripts/run_fix_loop.py`、`tests/test_resume.py` 等直接调用方使用，两套路径互不干扰。

### 4.3 GET /runs/{id}（v1.10.0 聚合响应）

`SqliteStateStore.get_run`（statestore/sqlite.py:130）返回 runs 表行（原始记录），API 层在其上做聚合：

```jsonc
{
  "run_id": "...", "workflow": "bug-fix-pipeline",
  "graph": { "name": "...", "nodes": [{id, agent, kind}], "edges": [{from, to, when}] },
  "status": "waiting_approval",
  "total_tokens": 0, "total_cost": 0.0,
  "nodes": {
    "triage": { "status": "done", "output": {...}, "params": {...},
                "tokens": 0, "cost": 0.0, "prompt": "..." }
  },
  "pending_approvals": [
    { "node_id": "approve-changes", "trigger": null,
      "upstream": { "fix": {...} } }
  ]
}
```

聚合逻辑（api/app.py `get_run`）：图从 snapshot yaml 重建（workflow 删除不影响已跑 run）；节点 checkpoint 合并 `tokens/cost/prompt`；`pending_approvals` 取 status==`waiting_approval` 的节点 + 上游输出。

> 字段级说明：`tokens/cost/prompt` 在 **API 模式（mock node_runner）诚实为 0/空**——mock 无 LLM 计量；真实 node_runner 接入后自动带真实值。

### 4.4 workflow YAML 格式（multi-agent-workflow）

`core/workflow.py` + `core/dag.py` 的 `Node`：

```yaml
name: bug-fix-pipeline
version: "1.0.0"
description: "..."
inputs: { bug_report: {type: object, required: true} }
nodes:
  triage:   { agent: triage, params: { bug: "$.inputs.bug_report" }, on_failure: abort }
  fix:      { agent: fix-implementer, params: {...}, retry: 2 }
  remediate:{ agent: infra-remediator, params: {...} }
  approve-changes:
    kind: approval            # ← 审批是独立节点（agent 字段省略）
    name: "审批修复方案"
    join: all
    required_edges: [fix, remediate]
    params: { diff: "$.nodes.fix.output.diff", ... }
    approvers: ["lead-engineer", "security-team"]
    timeout: 3600
    on_reject: abort
edges:
  - { from: fix, to: approve-changes }
  - { from: test, to: review, when: "$.nodes.test.output.passed == true" }
```

- 节点字段：`kind / agent / when / join(any|all) / required_edges / retry / params / on_failure / on_reject`。
- **审批模型**：独立的 `kind: approval` 节点（带 approvers/timeout/on_reject/join）。
- DAG 语义更丰富：`join: all`、SKIPPED 级联、条件边 `when`、审批 CAS + 终态不可逆、幂等（execution_id）、Resume。

---

## 5. 两者 workflow YAML 兼容性

| 维度 | agentflow 后端 | multi-agent-workflow | 结论 |
|---|---|---|---|
| 顶层结构 | name / inputs / nodes / edges | name / version / description / inputs / nodes / edges | 基本同构，超集 |
| 审批模型 | agent 节点 `approve: high-risk` 触发 | 独立 `kind: approval` 节点 | **不同** |
| 节点字段 | 含 `approve/on_schema_error/idempotency_key/input_view` | 不含这些；含 `join/required_edges/on_reject` | **字段集不同** |
| JSONPath 引用 | `$.nodes.X.output` / `$.nodes.X.output.field` | 相同 + 强校验（只能引用传递上游） | 语义一致 |
| 边 | `{from, to, when}` | 相同 | ✅ |
| 未知字段容忍 | pydantic `NodeDef` **会报错**（agentflow 后端保存 YAML 时校验） | `DAG.build` **静默丢弃**未知字段 | 单向不兼容 |

> 结论：multi-agent-workflow 的 YAML 不能直接保存到 agentflow 后端的 `/workflows`（approval 节点、join 等字段会被 pydantic 拒绝）；反之 agentflow 的 YAML（`approve:` 触发）在 multi-agent-workflow 里会丢失审批语义（`approve` 字段被静默丢弃，节点变成普通 agent 节点）。

---

## 6. multi-agent-workflow 对接前端所需的 8 个差距

| # | 差距 | 前端依赖（agentflow-ui） | multi-agent-workflow 现状 | 落地（v1.10.0） |
|---|---|---|---|---|
| 1 | **缺 workflow CRUD** | `POST/GET/PUT/DELETE /workflows`（流程配置页核心） | 无。workflow 是 `workflows/*.yaml` 文件，无 DB 存储 / id 复用 | ✅ v1.9.0：WorkflowStore + 6 端点 |
| 2 | **POST /run 请求契约不同** | body `{workflow_id, ticket}` | body `{workflow_yaml, inputs, tenant_id}` | ✅ 已兼容 `{workflow_id, ticket}`，保留旧字段 |
| 3 | **POST /run 同步阻塞** | 期望立即返回 `run_id` 再轮询 | `create_run` 阻塞到跑完才返回 | ✅ `start_run` + `asyncio.create_task` 后台执行 |
| 4 | **GET /runs/{id} 响应缺聚合** | `total_tokens/total_cost/nodes/pending_approvals` | 返回原始 runs 表行，无聚合 | ✅ 聚合：graph + nodes + 统计 + pending |
| 5 | **缺 /runs/{id}/reject** | 驳回审批按钮 | 无（只有 approve + `approved` flag） | ✅ 新增 `POST /runs/{id}/reject` |
| 6 | **缺 /runs/{id}/stop** | 停止按钮 | 无 | ✅ 新增 `POST /runs/{id}/stop` |
| 7 | **approve 签名不同** | body `{node_id}` | query `node_id` + body `{approved, by, comment}` | ✅ body `{node_id}`，保留 query 兼容 |
| 8 | **节点状态缺 tokens/cost/prompt** | 节点详情展示 LLM 计量与 prompt | 节点 cp 只有 `{status, output, params}` | ✅ 聚合层补 `tokens/cost/prompt`（API mock 诚实为 0） |

**可直接复用**：`GET /agents`（前端只用 name/description/tools，`stage` 多余无害）；`GET /health`；CORS 已配置；FastAPI 框架一致。

---

## 7. 适配方向（供后续决策，未实施）

- **方案 A：给 multi-agent-workflow 加「UI 兼容层」**（推荐，改动集中在本仓库 API 层）
  1. ✅ **已落地（v1.9.0）**：新增 `workflows` 表 + `/workflows` CRUD（保存 YAML + `_workflow_graph` 提取 graph）。
  2. ✅ **已落地（v1.10.0）**：改 `POST /run` 为异步（`asyncio.create_task`），支持 `workflow_id/ticket` 入参。
  3. ✅ **已落地（v1.10.0）**：新增聚合 `GET /runs/{id}`（合并 nodes 表 cp + 统计 token/cost + pending）。
  4. ✅ **已落地（v1.10.0）**：补 `/runs/{id}/reject`、`/runs/{id}/stop`；对齐 approve 签名为 body `{node_id}`。
  5. ✅ **已落地（v1.10.0，API 模式）**：节点 cp 增加 `tokens/cost/prompt` 字段——mock 执行器诚实返回 0（不伪造 LLM 计量）；真实 node_runner 接入后自动带真实值。
- **方案 B：改前端 `api.js`** 适配 multi-agent-workflow 契约（改动在 `agentflow-ui`，跨仓库）。
- **方案 C：维持现状** — UI 继续用 `agentflow` 后端；multi-agent-workflow 作为下一代引擎，后续做 workflow YAML 迁移（`kind: approval` 转换等）。

---

## 8. 附录：关键文件索引

### agentflow-ui（前端）
- `src/api.js` — 全部接口封装
- `src/components/WorkflowConfig.vue` — 流程配置页（YAML 粘贴 / 预览 / 保存 / 编辑 / 删除）
- `src/components/BugSolve.vue` — 运行 + 轮询 + 审批 + 节点详情
- `src/components/AgentsList.vue` — agents 列表
- `vite.config.js` — dev 代理到 localhost:8000

### agentflow（前端当前后端）
- `agentflow/server.py` — 全部 REST 端点（WorkflowStore / run 异步 / 审批 / stop）
- `agentflow/workflow/schema.py` — WorkflowDef（YAML 校验）
- `agentflow/workflow/parser.py` — YAML 解析 + JSONPath 校验
- `agentflow/engine/state.py` / `approval.py` — run 状态 / 审批管理器
- `examples/bug-fix-pipeline.yaml` — 前端可保存的 YAML 示例

### multi-agent-workflow（本仓库）
- `agentflow/api/app.py` — 控制面 FastAPI（当前端点）
- `agentflow/service.py` — RunService（同步 create_run / approve / resume）
- `agentflow/core/workflow.py` — Workflow 模型 + 版本冻结
- `agentflow/core/dag.py` — DAG 语义（join/skip/审批节点）
- `agentflow/statestore/base.py` / `sqlite.py` — 表结构 + get_run 原始记录
- `agentflow/executor/dag_executor.py` — 节点状态 `{status, output, params}`
- `workflows/bug-fix-pipeline.yaml` — 本仓库 YAML 示例（kind: approval 模型）

---

## 9. MCP Server 配置端点契约（v1.11.0）

> SIP「MCP Server 配置」页调用的后端：配置**通用的 MCP server**（stdio / streamable HTTP / SSE）并
> **绑定给 agent**，agent 运行时经 AgentScope `MCPClient` 把 MCP 工具与 function tool **共存**
> （hybrid toolkit）。存储/运行时实现与「测试连接」/安全注意详见
> `docs/MCP_SERVER_CONFIG_zh-CN.md`。

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| POST | `/mcp-servers` | `{name, transport, config, is_stateful?, agents, enable_tools?, disable_tools?, enabled}` | `201 {id}` | 落库 + 热刷新 client。`name` 须 `^[a-zA-Z0-9_-]+$` 且唯一；`transport ∈ stdio\|http`；stdio 强制 `is_stateful=true`（缺 `command`/http 缺 `url`/重复名 → 400 中文）。config 为嵌套 JSON（stdio 存 `command/args/env/cwd`，http 存 `url/headers/timeout`） |
| GET | `/mcp-servers` | — | `[{id, name, transport, config, is_stateful, agents, enable_tools, disable_tools, enabled, created_at, updated_at}]` | 全部记录（created_at 倒序） |
| GET | `/mcp-servers/{mid}` | — | 单条（同 GET 列表元素）| 不存在 → 404 |
| PUT | `/mcp-servers/{mid}` | 同 POST body | `{ok, id}` | 更新 + 热刷新（`enabled=false` 或删行 → 只 evict 不重建）。不存在 → 404 |
| DELETE | `/mcp-servers/{mid}` | — | `{ok: true}` | 删除 + evict（关 stateful 连接/杀 stdio 子进程）。不存在 → 404 |
| POST | `/mcp-servers/test` | `{transport, config, is_stateful?, enable_tools?, disable_tools?}` | `{ok, transport, tools:[{name, description, read_only, llm_name}], error?}` | **不落库**。临时建 client 连一次列工具；连不上/超时(10s)/配置坏 → `{ok:false, error}`（不抛 500）。注册在 `/{mid}` 系列之前 |
| GET | `/mcp-servers/{mid}/tools` | — | 同 `/test` 响应 | 已存 server 实时连接列工具（走 `MCPClientManager.test_connection`）。不存在 → 404 |

- `llm_name` = AgentScope 侧精确工具名 `mcp__{server}__{sanitized_tool}`（含 sanitize：如 `query.repo` → `queryxrepo`），前端可展示；allow 名单按此精确匹配。
- `read_only` = 是否携带只读标注（true → 运行时自动 ALLOW；false → 需 allow 规则）。
- 前端「agents 多选」数据源：`GET /agents`（本仓库 `AGENT_REGISTRY`）。
