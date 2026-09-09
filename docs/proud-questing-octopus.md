# 后端配置 MCP Server + Agent 绑定（替代/共存 function tool）

## Context

**问题**：当前 agent 的工具是硬编码绑定 function tool（`TOOL_REGISTRY` → `build_l1/l2_tools` → AgentScope `FunctionTool`）。用户想要：后端一个配置页面，能配置**通用的 MCP server**（stdio / SSE / streamable HTTP 任意类型），并把 MCP server **绑定给 agent**，agent 通过 **MCP client** 调用 MCP 工具，与现有 function tool **共存**（LLM 两类工具都能调用）。

**已确认决策**：
- MCP 工具与 function tool **共存**（hybrid toolkit），不替换。
- 交付范围 = **本仓库后端 API + SIP 前端页面**（跨仓库）。
- 计划内附带一个 **mock MCP server** 用于 pytest / E2E（另有真实 HTTP 目标：sibling `aiops-mcp-servers/git-mcp-server`）。

**关键使能点（探索已证实）**：
- AgentScope 2.0.3 已原生支持 MCP：`agentscope.mcp.MCPClient`（stdio=stateful 必须 / HTTP 可 stateless 或 stateful，URL 以 `/sse` `/messages/` 结尾自动走 SSE，否则 streamable HTTP）+ `Toolkit(mcps=[...])` 自动合并工具。`mcp==2.1.1` 已随 agentscope 装好，**无需新依赖**。
- 现有接缝 `agentflow/agents/mcp.py:build_toolkit(..., mcp_clients=...)` 已存在 `Toolkit(tools=[], mcps=[...])` 分支，只是没人传 client。
- MCP 工具对 LLM 的名字是 `mcp__{server}__{sanitized_tool}`（venv `_adapters.py:219`）。
- Permission 是 **DONT_ASK + allow 规则按精确工具名**匹配（venv `_engine.py:624`）；非只读 MCP 工具无 allow 规则会被 DENY。只读 MCP 工具（`read_only_hint`）自动 ALLOW。
- 后端 CRUD 可直接复刻 `agentflow/api/workflow_store.py`（aiosqlite 惰性连接）。
- 前端可直接复刻 SIP 的 Workflow Studio 页（`initWorkflowStudio()` app.js:5389-5586）。
- ⚠️ 本仓库 venv 是 **mcp 2.x**：`from mcp.server.fastmcp import FastMCP` **不可用**（已实测 ModuleNotFoundError），mock server 必须用 `mcp.server.mcpserver.MCPServer`。跨版本 wire 协议兼容，sibling `git-mcp-server`（mcp v1、FastMCP、streamable HTTP `/mcp` + Bearer）仍是有效真实联调目标。

---

## 后端改动（multi-agent-workflow 仓库）

### 1. 新建 `agentflow/api/mcp_store.py` — `MCPStore`（复刻 `workflow_store.py`）
表 `mcp_servers`：
```sql
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,      -- 也是 MCPClient.name，须 ^[a-zA-Z0-9_-]+$
    transport     TEXT NOT NULL,             -- 'stdio' | 'http'
    config        TEXT NOT NULL,             -- JSON：stdio{command,args,env,cwd} / http{url,headers,timeout}
    is_stateful   INTEGER NOT NULL DEFAULT 1,-- stdio 强制 1；http 默认 0(stateless)
    agents        TEXT NOT NULL,             -- JSON list[str]：绑定的 agent 名
    enable_tools  TEXT,                      -- JSON list[str] | null
    disable_tools TEXT,                      -- JSON list[str] | null
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```
方法镜像 WorkflowStore：`__init__(db_path)` / 幂等 `connect()` / `_c` / `close()` / `save(row)->id` / `list()` / `list_enabled()` / `get(mid)` / `update(mid,row)->bool` / `delete(mid)->bool`（用 `rowcount` 作 miss 哨兵）。

### 2. 新建 `agentflow/agents/mcp_manager.py` — `MCPClientManager(store)`
- 构造函数**不做 DB/I/O**（保证 ASGITransport 测试可在 import 后 monkeypatch）。
- `_build_client(row) -> agentscope.mcp.MCPClient`：config JSON → `StdioMCPConfig`/`HttpMCPConfig`；`is_stateful`（stdio 强制 True）；`enable_tools/disable_tools`。
- `async load()`：读 enabled 行建 client；stateful **best-effort connect**（失败 log 并跳过，不阻塞启动）。
- 缓存 `_clients: dict[mid, MCPClient]`、`_rows: dict[mid, dict]`。
- `async clients_for_agent(agent_name) -> list[MCPClient]`：enabled 且 `agents` 含该 agent 的 client；stateful 若失联做**一次重连**再返回。
- `async allow_names_for_agent(agent_name) -> list[str]`：对绑定 client `await client.list_tools()`，经 `client.get_tool(name).name` 取 AgentScope 命名（避免 sanitize 漂移），返回 `mcp__{server}__{tool}` 精确名；按 client 缓存。
- `async test_connection(row) -> dict`：临时建 client（不落库）；stateful→connect+list+close，stateless→靠 list_tools 临时会话；包 `asyncio.wait_for(...,10)`。返回 `{ok, transport, tools:[{name,description,read_only,llm_name}], error?}`。
- `async refresh_server(mid)` / evict：CRUD 后重建（先 close 旧 stateful 再换）；`async close_all()` 关闭 stateful（杀 stdio 子进程）。

### 3. 改 `agentflow/agents/mcp.py` `build_toolkit` → **hybrid**（向后兼容）
- 现有调用方（scripts / tests / runner）都只传 `use_mock=True` 且不带 `mcp_clients`，无 break。
- 新逻辑：始终按 `use_mock` 构建 function tools（L1 mock/real + L2）；若有 `mcp_clients` → `Toolkit(tools=func_tools, mcps=list(mcp_clients))`；否则 `Toolkit(tools=func_tools)`。**防御性过滤**掉 stateful-but-not-connected 的 client（避免 `Toolkit.__init__` 对未连接 stateful 抛 ValueError，venv `_toolkit.py:145-151`）。

### 4. 改 `agentflow/agents/runner.py` `AgentNodeRunner`
- `__init__(model, *, use_mock_datasource=True, mcp_manager=None)`。
- `__call__(node, params)`：有 agent 且有 mcp_manager 时 →
  1. `clients = await mcp_manager.clients_for_agent(agent)`
  2. `allow_extra = await mcp_manager.allow_names_for_agent(agent)`
  3. `toolkit = build_toolkit(agent, use_mock=..., mcp_clients=clients)`
  4. `ctx = build_permission_context(agent, allow_extra=allow_extra)`（allow 规则**先于 build_agent**，保证首个工具调用前生效）
  5. `build_agent(agent, toolkit, self.model, permission_context=ctx, ...)`
- 无 mcp_manager 时维持现状。

### 5. 改 `agentflow/api/app.py`
- 模块级：`mcp_store = MCPStore(settings.state_db_path)`、`mcp_manager = MCPClientManager(mcp_store)`（import 时构造，供测试 monkeypatch）。
- Pydantic body：stdio/http config 变体 + `MCPServerCreate/Update`（含 agents、enable/disable_tools、enabled）+ `MCPTestRequest`。
- `init()`：`await mcp_store.connect()`；`await mcp_manager.load()`；有 DEEPSEEK key 时 `AgentNodeRunner(build_model(settings), mcp_manager=mcp_manager)`。
- `@app.on_event("shutdown")`：`await mcp_manager.close_all()`。
- 端点（**静态子路径 `/test` 先于 `/{mid}` 注册**，同 `/workflows/preview` 教训）：
  - `POST /mcp-servers`（name 校验 charset/唯一、transport 校验、stdio 强制 stateful）→ 201
  - `GET /mcp-servers` / `GET /mcp-servers/{mid}`（404）
  - `PUT /mcp-servers/{mid}` → update + `await mcp_manager.refresh_server(mid)`（404）
  - `DELETE /mcp-servers/{mid}` → delete + evict（404）
  - `POST /mcp-servers/test`（body=config，不落库）→ `{ok,tools,error}`；配置解析失败 → 400 中文
  - `GET /mcp-servers/{mid}/tools` → 已存 server 连接并 list，同形状
- 400 中文错误、graph 式校验复刻 workflow 端点；CRUD 后 `mcp_manager` 热刷新。

### 6. 新建 `scripts/mock_mcp_server.py`（mcp 2.x `MCPServer`，非 FastMCP）
- `from mcp.server.mcpserver import MCPServer`；`@server.tool()` 注册覆盖三种权限形态：
  - `get_weather(city)` 带 `read_only_hint=True`（auto-ALLOW 路径）
  - `query.repo(...)`（含 `.`，验证 sanitize → `mcp__mock-mcp__queryXrepo`）
  - `send_alert(...)` 无只读标注（需 allow 规则）
- `server.run(transport="stdio")` 为主；附 `--http`（`run_streamable_http_async`）供 E2E。
- pytest 用 `StdioMCPConfig(command=sys.executable, args=["<abs>scripts/mock_mcp_server.py"])` 起子进程（scripts/ 非包，不能 `-m`）；teardown `close_all()` + kill 兜底。

> 真实 HTTP 验证可另起 sibling `aiops-mcp-servers/servers/git-mcp-server`（mcp v1 FastMCP、`uv run python -m git_mcp_server` + `AUTH_TOKEN`、url `http://127.0.0.1:8000/mcp`），作为真实 server 的 E2E 目标。

### 7. `config.py` — 无需改
`MCPStore` 复用现有 `settings.state_db_path`（与 WorkflowStore 同库异表）。可选留白：`mcp_servers_connect_on_startup` 开关（本期不做）。

---

## 后端测试

- `tests/test_mcp_store.py`：CRUD 往返 / list 排序 / update·delete 哨兵 / enabled 过滤。
- `tests/test_mcp_api.py`：复刻 `test_workflow_api.py`（`ASGITransport` + monkeypatch `app_mod.mcp_store`/`mcp_manager` fixture）。用例：CRUD/404/非法 transport→400/`/test` 连不上→`{ok:false}` 不崩/name charset。
- `tests/test_mcp_manager.py`：stdio client 指向 mock server 子进程；`clients_for_agent` 绑定过滤；`allow_names_for_agent` 返回 `mcp__mock-mcp__...`；`test_connection` 返回 3 工具。
- `tests/test_mcp_integration.py`：`AgentNodeRunner(mcp_manager=...)` + `ScriptedJsonModel` + mock stdio server → bound agent toolkit 含 MCP 工具，ScriptedJsonModel 首个工具调用命中 MCP 工具并真实执行（验证 `MCPTool.call` 会话链路）。
- 每步跑 `make test` 保持全绿。

---

## 前端改动（SIP 仓库 `service-intelligence-platform-ui`，复刻 Workflow Studio）

- `index.html`：`#agent-fleet` 内加 `<div class="sidebar-item" data-page="mcp-servers">MCP Server 配置</div>`（~:141 后）；克隆 `#page-workflow-studio`（~:1418-1476）加 `#page-mcp-servers`：左列表单（name / transport select / 对应 config 字段或 JSON / is_stateful(http) / agents 多选，数据来自 `GET /agents` / enable·disable_tools）+「测试连接」按钮 + 错误条 + 已存列表；右列显示测试/工具列表。
- `js/app.js`：路由两处分支（`initSidebar` ~:109-110、`initNavigation` Agents ~:602）；新增 `initMcpServers()`（镜像 `initWorkflowStudio()` app.js:5389-5586）：`MCP_API_BASE = localStorage['mcpBaseUrl'] || 'http://localhost:8000'`；loadMcpServers / renderList / testConnection / save / edit / remove / resetForm / bindEvents（列表事件委托）；400 读 `data.detail`；注册 `_pageRefreshers['page-mcp-servers']`；`init()` 注册（~:6005）。
- `css/styles.css`：ws 块（~:5260）后追加 `mcp-` 前缀样式，复用 `.apm-field` / `.si-refresh-btn` / `.btn*` / CSS 变量。
- `changelogs/v1.11.0-mcp-server-config.md`：按 v1.9.0 格式（改明细/后端配套/影响范围表/版本说明 MINOR）。

---

## 文档

- `docs/AGENTFLOW_UI_INTEGRATION_RESEARCH_zh-CN.md`：追加 `/mcp-servers` 端点契约表（方法/路径/请求/响应）。
- 新建 `docs/MCP_SERVER_CONFIG_zh-CN.md`：transport 矩阵、绑定+共存语义、连接生命周期、安全注意（auth headers 明文落 SQLite config JSON → 建议后续 env 引用）、mock server 与真实 `git-mcp-server` 接入示例。

---

## 验证（E2E）

1. `make test` 全绿（新旧用例）；`make lint`。
2. `make api`（`source ../spike/.env` 供 DEEPSEEK_API_KEY）。
3. curl 联调：
   - `POST /mcp-servers/test` body `{transport:'stdio', is_stateful:true, config:{command:'<abs python>', args:['<abs>/scripts/mock_mcp_server.py']}}` → `{ok:true, tools:[get_weather, query.repo, send_alert]}`。
   - 起 sibling git-mcp-server 后，body `{transport:'http', is_stateful:false, config:{url:'http://127.0.0.1:8000/mcp', headers:{Authorization:'Bearer <token>'}}}` → 6 个 git 工具。
4. `POST /mcp-servers` 落库并绑 `triage` → `POST /run`（工作流含 triage 节点）→ 日志见 MCP 工具真实调用（需真实 LLM key）。
5. 前端 E2E（SIP）：`python -m http.server 8080` + 后端 8000 → 打开 MCP Server 配置页：新增→测试连接出工具→保存→列表可见→绑 agent→删除。
6. 升级注意：agentscope 版本冻结约束（CLAUDE.md §1）——本期零升级，仅用既有 MCP 能力。

---

## 关键风险 / 注意点

- **mcp 2.x 无 FastMCP** → mock 用 `MCPServer`；跨版本 wire OK，真实 HTTP 可用 mcp v1 的 git-mcp-server。
- `Toolkit.__init__` 对未连接 stateful 抛 ValueError → manager load 时 connect + 一次重连 + hybrid 过滤兜底。
- `_get_available_tools` 每轮迭代都 `client.list_tools()`（venv `_toolkit.py:519`）→ 每 client 每轮一次 I/O；热 server 建议 stateful HTTP；写入文档。
- allow 规则精确匹配且须先于首个工具调用 → 在 build_agent 前用 manager 预取。
- sanitize 碰撞（`foo.bar` vs `foo:bar` → `fooXbar`）→ allow 名单用 `client.get_tool().name` 避免漂移。
- 明文 secrets（headers）落 SQLite → 文档提示，后续可 env 引用。
- stdio 子进程泄漏 → shutdown `close_all()`；refresh 先 close 旧 client；测试 kill 兜底。
- 路由顺序 `/mcp-servers/test` 先于 `/{mid}`。
- ASGITransport 不触发 lifespan → 端点惰性连接 + 测试 monkeypatch 模块全局（store+manager）。
- 并发 run 共享同一缓存 stateful session → 不在 run 中途 refresh（CRUD 时才刷新）。

## 关键文件清单

| 文件 | 动作 |
|---|---|
| `agentflow/api/mcp_store.py` | 新建（复刻 workflow_store.py） |
| `agentflow/agents/mcp_manager.py` | 新建 |
| `scripts/mock_mcp_server.py` | 新建（MCPServer stdio/http） |
| `agentflow/agents/mcp.py` | 改 build_toolkit → hybrid |
| `agentflow/agents/runner.py` | 改 AgentNodeRunner 注入 mcp_manager |
| `agentflow/api/app.py` | 加 globals/init/shutdown + 6 端点 |
| `tests/test_mcp_{store,api,manager,integration}.py` | 新建 |
| `docs/AGENTFLOW_UI_INTEGRATION_RESEARCH_zh-CN.md`、新建 `docs/MCP_SERVER_CONFIG_zh-CN.md` | 改/新建 |
| SIP: `index.html` / `js/app.js` / `css/styles.css` / `changelogs/v1.11.0-mcp-server-config.md` | 改（复刻 Workflow Studio） |
