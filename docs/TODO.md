# TODO（待办清单）

> 目标：把 agentflow 从"单项目写死"演进成"可配置平台"。优先做高优先级项。
> 每项带「现状 / 问题 / 目标 / 涉及文件」，方便接手。

---

## 1. ⭐ Agent 配置可配置化（平台化关键）—— ✅ 主体落地 v1.12（2026-09-03）

> **落地情况（v1.12）**：DB 驱动 agent 配置（`agent_configs` 表 sqlite+PG）+ `AgentConfigResolver` 合并解析 +
> 控制面 `/agent-configs` CRUD + agent→MCP server 绑定（server 粒度）+ SIP「Agent 配置」页。
> 见 `docs/AGENT_CONFIG_DB_zh-CN.md`。**尚未覆盖**：tools 可见性 / 超时 / 限流、按租户模型参数（下表第 4 行起）。
>
> **现状**（v1.12 前的硬编码基线）：15 个 agent 的全部配置**硬编码**在项目代码里：

| 配置 | 位置 |
|---|---|
| 名字 / 角色 / 描述 | `agentflow/agents/registry.py`（`DIAGNOSE_AGENTS` / `FIX_AGENTS` 列表 + `AGENT_DESCRIPTIONS`） |
| 提示词 / 输出契约 | `agentflow/agents/prompts.py`（`SYSTEM_PROMPTS` / `AGENT_SCHEMAS`） |
| 工具可见性 / 超时 / 限流 | `agentflow/agents/tools.py`（`TOOL_REGISTRY` 每个 `ToolSpec.agents` 列表） |

**问题**：做平台必须支持租户/运营侧**配置**，而非改代码发布。典型需求：

- 新增 / 停用 / 修改 agent
- 自定义 system prompt / 输出 schema
- 调整某个 agent 可见的工具（授权矩阵）
- 按租户覆盖模型参数（model / max_iters / 是否真实 LLM）

**目标**：仿照 `workflows/*.yaml` 的声明式模式，提供 `agents/*.yaml`（或 DB 驱动）配置，注册表**从配置构建**；`AGENT_REGISTRY` 从"import 时写死"变为"运行时装配"。

**涉及**：`agentflow/agents/registry.py`、`prompts.py`、`tools.py`、（新增）配置加载层。

---

## 2. 真实 node_runner 接入 executor —— ✅ 已完成（383b6b7 + v5.3 批C）

`agents/runner.py:AgentNodeRunner` 经 `RunService(node_runner=...)` 注入 executor/API；有 `DEEPSEEK_API_KEY` 即真实 LLM（无 Key 回退 mock）。v5.3 批 C 进一步：按 `current_tenant` 路由 per-tenant MCP / agent 配置；`AGENTFLOW_SHARED_DATASOURCES=0`（默认加固）时不注入内置共享数据源工具。剩余：L2 沙箱工具接入真实 run。

## 3. `/agents` 端点增强

- 可选 `?role=diagnose|fix` 过滤
- 工具返回元数据（`level` L1/L2、`needs_approval`）而非只有工具名

---

## 4. 清预置 lint / 测试债

- `make lint` 目前有 ~140 个预置 ruff 错误（改动前后不变，非本次引入）
- `tests/test_workspace.py`、`tests/test_agents.py::test_code_locator_cmdb_driven` 引用不存在的 `agentflow.workspace`（M3 模块未落树）→ 收集报错中断全量测试
- `tests/test_sandbox.py` 5 个用例依赖本机 `~/.kube/config`
