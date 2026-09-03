# MCP Server 配置（运行时下发给 agent）

> 后端实现 v1.11.0（本仓库 `multi-agent-workflow`）。配套前端页见 SIP `service-intelligence-platform-ui`
> `changelogs/v1.11.0-mcp-server-config.md`；端点契约见 `AGENTFLOW_UI_INTEGRATION_RESEARCH_zh-CN.md` §9。
> **v1.11.x 起 server 记录不再存 `agents` 绑定字段**（agent 侧绑定后续以 **agent 为主表**建模，MCP server
> 不再是绑定主表）：运行时把全部 enabled server 下发给每个 agent。存储后端跟随 `state_store`
> （sqlite 默认 / postgres 生产）。

## 1. 一句话

后端在 SIP 页配置一条**通用的 MCP server**（stdio / streamable HTTP / SSE）；agent 运行时把 MCP 工具与
既有 **function tool（L1/L2）共存**（hybrid toolkit）——同一 agent 在 AgentScope react 循环里既能调内置
只读/执行工具，也能调下发的 MCP server 工具（当前为**全部 enabled server**，不做 per-server agent 过滤）。

## 2. 存储与运行时分层

| 层 | 文件 | 职责 |
|---|---|---|
| CRUD 存储 | `agentflow/api/mcp_store.py` `MCPStore` / `PgMCPStore` | `mcp_servers` 表（sqlite 与 workflow/state 同库异表；postgres 生产同库），aiosqlite / psycopg3 惰性连接；旧库迁移会补 `tools` 列并 DROP 废弃的 `agents` 列 |
| 运行时 | `agentflow/agents/mcp_manager.py` `MCPClientManager` | 配置 JSON → AgentScope `MCPClient`；加载/热刷新/evict/close_all；失联重连；allow 名单预计算；`test_connection`（**无 per-server agent 绑定**） |
| 组装 | `agentflow/agents/mcp.py` `build_toolkit` | hybrid：function tool + MCP client 合并；防御性剔除「stateful 但未 connect」client |
| 接入 | `agentflow/agents/runner.py` `AgentNodeRunner` | 每节点取 `clients_for_agent` + `allow_names_for_agent`（全部 enabled）→ `build_toolkit(..., mcp_clients)` + `build_permission_context(allow_extra=...)` |
| API | `agentflow/api/app.py` | `/mcp-servers` CRUD + `/test` + `/{mid}/tools`（§9） |

## 3. transport 矩阵

| transport | config 字段 | `is_stateful` | 说明 |
|---|---|---|---|
| `stdio` | `{command, args?, env?, cwd?}` | **强制 true** | AgentScope 拉起子进程 + 持久 stdio 会话。适合本地 mock / 脚本类 server |
| `http`（streamable HTTP） | `{url, headers?, timeout?}` | 默认 false（stateless）| url 非 `/sse`/`/messages/` 结尾走 streamable HTTP；stateless 每次工具调用起临时会话，适合高并发/多 run 共享 |
| `http`（SSE 旧式） | 同上 | 建议 true（stateful）| url 以 `/sse` 或 `/messages/` 结尾自动走 SSE 传输；SSE 连接昂贵，建议保持 stateful 常连 |

判定逻辑在 AgentScope `MCPClient._create_http_client`：`config.url.endswith("/sse") or "/messages/"` → SSE，
否则 streamable HTTP。**热 server 建议 stateful http**——`_get_available_tools` 每轮迭代都会对每个 client
做一次 `list_tools()` I/O（stateless 每次还会起临时会话），stateful 常连成本更低。

## 4. 下发 + 共存语义

- **下发（无 per-server 绑定）**：server 记录不含 `agents` 字段（已移除）。运行时
  `MCPClientManager.clients_for_agent(name)` 返回**全部 enabled** 且已连接的 client——任意 agent 名都可见。
  复用同一 client/session（并发 run 共享，**不在 run 中途刷新**）。后续 agent 侧绑定落地后，将按 agent 所选
  server 子集过滤（方法签名保留 `agent_name` 作接入缝）。
- **共存（hybrid）**：`build_toolkit` 永远按 `use_mock` 构建 function tools（L1 只读 + L2 执行），有 client 时再
  `Toolkit(tools=[...], mcps=[...])` 叠加 MCP 工具。两类工具的 schema 一起给 LLM，工具名前缀区分 function tool
  （原名）与 MCP 工具（`mcp__{server}__{tool}`）。
- **权限（§9.5 DONT_ASK + 精确 allow）**：
  - 只读 MCP 工具（server 声明 `readOnlyHint`，mock `get_weather`/`query.repo`）→ AgentScope 在
    `MCPTool.check_permissions` **自动 ALLOW**，无需规则。
  - 非只读 MCP 工具（mock `send_alert`）→ 需 **allow 规则精确命中 LLM 侧名**，否则 DENY。
  - `AgentNodeRunner` 在 `build_agent` **之前**用 `allow_names_for_agent` 预取（当前为全部 enabled client）
    的 `mcp__...` 名，注入 `build_permission_context(allow_extra=...)`（必须早于首个工具调用）。
- **向后兼容**：既有调用方（scripts / tests / runner）不传 `mcp_clients` → 结果与旧行为一致（纯 function tools）。

## 5. 连接生命周期

```
启动:  app.init() → mcp_store.connect() + mcp_manager.load()
        load(): list_enabled() → 每条建 MCPClient；stateful best-effort connect（失败 log 跳过，不阻塞启动）
CRUD:   POST / PUT / DELETE /mcp-servers[/{mid}] → refresh_server(mid)
        refresh: 先 evict（close 旧 stateful，杀 stdio 子进程）→ 从库里当前行重建 + connect；
                 enabled=0 或行已删 → 只 evict
运行:   clients_for_agent(): stateful 失联 → 一次重连，仍失败则本次 run 跳过该 server
关闭:   shutdown → close_all()（close 全部 stateful）
```

要点：
- `Toolkit.__init__` 对「stateful 但未 connect」的 client 抛 ValueError → `build_toolkit` 再做一层防御过滤。
- stdio 子进程在 `close()`/`evict`/`shutdown` 时终止；测试断言后也 `close_all()`。

## 6. 安全注意

- **auth headers 明文落 SQLite**：`config.headers`（如 Bearer）以 JSON 明文存在 `mcp_servers.config`。
  仅本地控制面/内网可访问；后续建议支持 env 引用（`${ENV_VAR}`）以复用网关托管密钥。
- 工具权限由 allow 规则兜底；非只读 MCP 工具在 DONT_ASK 下默认 DENY。当前下发是「全部 enabled
  server → 每个 agent」，非只读工具全量进 allow 名单；agent 侧绑定落地后应按所选 server 子集收紧。
- name 校验 `^[a-zA-Z0-9_-]+$`（同时是 LLM 工具名前缀，硬性约束）。

## 7. Mock server 与真实 server 接入示例

### 7.1 本仓库 mock（stdio，pytest/前端「测试连接」联调）

`scripts/mock_mcp_server.py`（mcp v1 `FastMCP`）三个工具覆盖三种权限形态：

```python
get_weather(city)                 # readOnlyHint=True → 只读自动 ALLOW
query.repo(repo)                  # 工具名含 '.' → 验证 sanitize：mcp__mock-mcp__queryxrepo
send_alert(title, severity='warning')  # 无只读标注 → 非只读需 allow 规则
```

「测试连接」body：

```jsonc
{
  "transport": "stdio",
  "is_stateful": true,
  "config": {
    "command": "/absolute/path/to/venv/bin/python",
    "args": ["/absolute/path/to/scripts/mock_mcp_server.py"]
  }
}
// → {ok:true, tools:[get_weather, query.repo, send_alert]}（read_only 与 llm_name 齐全）
```

### 7.2 真实 streamable HTTP（sibling `aiops-mcp-servers/servers/git-mcp-server`）

mcp v1 FastMCP + Bearer 鉴权，url `http://127.0.0.1:8000/mcp`。测试连接 body：

```jsonc
{
  "transport": "http",
  "is_stateful": false,          // stateless：每次调用临时会话
  "config": {
    "url": "http://127.0.0.1:8000/mcp",
    "headers": { "Authorization": "Bearer <AUTH_TOKEN>" }
  }
}
// → ok:true + 6 个 git 工具
```

落库（server 侧无 agent 绑定 → 全部 enabled 下发到每个 agent）后跑任一 workflow，日志应见
`mcp__git-server__...` 工具被真实调用（需真 LLM key）。

## 8. 测试

- `tests/test_mcp_store.py` — CRUD 往返 / enabled 过滤 / update·delete 哨兵 / 重名。
- `tests/test_mcp_api.py` — ASGITransport + monkeypatch store/manager：CRUD/404/校验 400/`/test` 不崩/name charset。
- `tests/test_mcp_manager.py` — 真实 stdio mock 子进程：`test_connection` 列 3 工具（含 sanitize 名与只读标注）、
  `clients_for_agent` 无绑定→全部 enabled、`allow_names_for_agent` 精确名、stateless HTTP live。
- `tests/test_mcp_integration.py` — `AgentNodeRunner(mcp_manager=...)` + 确定性模型 → LLM 首个工具调用命中 MCP
  工具并真实执行（验证 hybrid toolkit + `MCPTool.call` 会话链路）。
