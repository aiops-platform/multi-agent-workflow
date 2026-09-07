# Agent 配置 DB 驱动（AgentSpec 可配置化）—— v1.12 设计落地

> 目标：把 15 个硬编码 agent（`registry.py` / `prompts.py`）升级为**控制面 DB 配置 + 运行时合并解析**，
> 前端「Agent 配置」页可编辑内置、可新增/删除自定义 agent，且每个 agent 以 **server 粒度**选择要调用的 MCP server。
>
> 关联实现：`agentflow/api/agent_store.py`（存储）、`agentflow/agents/agent_config.py`（合并解析器）、
> `agentflow/api/app.py`（CRUD + init 接线 + `GET /agents` 合并视图）、`agentflow/agents/scopes.py::build_agent`、
> `agentflow/agents/runner.py::AgentNodeRunner`、`agentflow/agents/mcp_manager.py`（server_ids_for 过滤）、
> `docker/init/01_schema.sql`（PG 建表）。跨仓库前端：SIP `service-intelligence-platform-ui`（`changelogs/v1.12.0-agent-config.md`）。
>
> **v1.12.1 语义变更注记**：`mcp_server_ids` 由「三态（NULL=全量 enabled）」改为**两态（无/子集）**——
> 未配置（NULL）或 `[]` 一律 = **没有 MCP server**，需在配置页显式勾选绑定的 server 才有；去掉了
> 「未配置=全部 enabled」的默认。详见 SIP `changelogs/v1.12.1-agent-config-two-state-binding.md`。

---

## 1. 核心语义（先定，避免摇摆）

- **覆盖即存值、NULL 即回退内置**：`agent_configs` 行对内置 15 的 `description/system_prompt/schema_json` 存 `NULL`
  = 未覆盖 → 运行时回退静态 `AGENT_DESCRIPTIONS / SYSTEM_PROMPTS / AGENT_SCHEMAS`；写入非空即覆盖。
  **schema 覆盖仅为元数据/详情展示层生效**（对齐现状：`extract_json` 不做强校验，prompt 内联 JSON 模板不自动重写）。
- **`origin`**：`'builtin'`（init 从静态 registry seed，禁删，可编辑/清空覆盖回退）| `'custom'`（POST 新建，可删）。
- **`enabled=0`**：运行时该 agent 节点短路返回 `{"node":…,"ok":True,"disabled":True,"note":…}`，不调 LLM。
- **`mcp_server_ids` 两态（v1.12.1 起；见文末变更注记）**：
  - `NULL`（未配置）/`[]`（写入归一为 NULL）= **无 MCP server**（该 agent 没有 MCP 工具）；
  - `[mid,…]` = 精确子集。
  「没配置就等于没有 server」——**不再有「未配置=全量 enabled」的默认**；agent 需在配置页
  显式勾选绑定的 server，运行时才有 MCP 工具。
- **seed 幂等 + 内置默认物化**：init 时 `agent_configs` 表为空 → 从静态 registry/prompts seed 15 条 builtin
  （origin/role/stage/description/**system_prompt/schema_json** 均落静态默认，让 DB 成为内置 agent 的可见配置
  快照；mcp_server_ids 仍 NULL、enabled=1）。表非空不整库重灌，但会对**已存在的内置行**做缺失默认回填——
  若某内置行 system_prompt/schema_json 仍为 NULL（历史 seed 空列），补齐为当前静态默认；用户编辑过的非空值不动。
  ⚠️ 物化后改代码里 SYSTEM_PROMPTS/AGENT_SCHEMAS 不再自动生效；清空该字段保存（→NULL 即回退内置），
  下次启动回填会固化为当前默认。
- **name 约束**：`^[a-zA-Z0-9_-]+$`（name 是 workflow `node.agent` 引用键 + AgentScope `Agent.name`）；name 全库唯一。
  custom 必须提供 `role∈{diagnose,fix}` + 非空 `system_prompt`（`""`/空白 → NULL 归整，custom 归整后仍空 → 400）。

---

## 2. 存储 `agent_configs`（sqlite + PG 双后端）

`agentflow/api/agent_store.py`（照抄 `mcp_store.py` 骨架）：sqlite `_SCHEMA` + PG `_PG_SCHEMA`（两表结构一致，
JSON 存 TEXT、bool 存 INTEGER、时间戳 ISO TEXT），表 `agent_configs`：

```sql
name            TEXT PRIMARY KEY,          -- agent 名（= node.agent / SYSTEM_PROMPTS key）
origin          TEXT NOT NULL DEFAULT 'builtin',   -- 'builtin' | 'custom'
role            TEXT NOT NULL,             -- diagnose | fix
stage           TEXT NOT NULL DEFAULT 'other',     -- detect/diagnose/fix/verify/deliver/learn/other
description     TEXT,                      -- NULL → 回退 AGENT_DESCRIPTIONS
system_prompt   TEXT,                      -- NULL → 回退 SYSTEM_PROMPTS
schema_json     TEXT,                      -- NULL → 回退 AGENT_SCHEMAS（仅元数据/详情）
mcp_server_ids  TEXT,                      -- JSON list[str] | NULL；两态见 §1（NULL/[]=无 server）
enabled         INTEGER NOT NULL DEFAULT 1,
created_at TEXT NOT NULL, updated_at TEXT NOT NULL
```

- 复用 `_to_row/_from_row`：JSON 列读写、`""`/纯空白 → `None`（`_opt_str`）、bool → 0/1、ISO 时间戳。
- `save/update/delete/list/get`（list 按 `created_at DESC`）；save name 冲突 → `sqlite3.IntegrityError`
  （PG `UniqueViolation` → 归一化抛同异常，供 app 层 400「已存在」映射跨库生效）。
- `build_agent_config_store(settings)`：`state_store=postgres` → `PgAgentConfigStore`（psycopg3 async、惰性连），否则 sqlite。
- agent_configs 是新表，无需 mcp_servers 那种「ADD/DROP 列」迁移钩子。

---

## 3. 运行时合并解析 `AgentConfigResolver`（agents 层，不 import api）

`agentflow/agents/agent_config.py`（构造喂 store 行，启动/CRUD 后重建，避免 agents→api 反向依赖）：

```python
@dataclass
class ResolvedAgent:
    name, origin, role, stage, description, system_prompt, schema: dict, enabled: bool,
    mcp_server_ids: set[str]          # 空 set=无 server；非空=精确子集（两态）

class AgentConfigResolver:
    def __init__(self, rows: list[dict])            # 建 name→row 索引
    def resolve(self, name) -> ResolvedAgent | None # DB 覆盖 + 静态回退（NULL→AGENT_DESCRIPTIONS/SYSTEM_PROMPTS/AGENT_SCHEMAS）
    def server_ids_for(self, name) -> set[str]     # 空 set=无 server（两态）；非空=精确子集
    def all(self) -> list[ResolvedAgent]            # DB 行 ∪ 未落库内置；顺序 = DIAGNOSE+FIX 内置 → custom
    def get(self, name) -> dict | None              # 原始 DB 行（未合并）
```

静态合并来源：`agents/registry.py`（`AGENT_DESCRIPTIONS/AGENT_STAGES/DIAGNOSE_AGENTS/FIX_AGENTS`）+
`agents/prompts.py`（`SYSTEM_PROMPTS/AGENT_SCHEMAS`）。

---

## 4. 装配链（改动热生效）

| 接入点 | 文件 | 行为 |
|---|---|---|
| `build_agent` | `agents/scopes.py` | 新增 keyword `system_prompt: str | None = None` → `system_prompt or SYSTEM_PROMPTS.get(name, 兜底)` |
| `AgentNodeRunner` | `agents/runner.py` | `__init__` 增 `agent_config: AgentConfigResolver | None`；`__call__` 开头 `resolve(agent)`：`enabled=0` → 短路占位（不调 LLM）；否则合并后 `system_prompt` 传 `build_agent` |
| `MCPClientManager` | `agents/mcp_manager.py` | `__init__` 增 `server_ids_for`（注入 resolver 的 `server_ids_for`，返回空 set=无 server）；`clients_for_agent` 注入后对 enabled client 按 `mid in allowed` 收窄（**未注入（None）→ 全部 enabled 现状兼容**）；`allow_names_for_agent` 复用（非只读 allow 只对真正下发的 client 生成） |
| `init()` | `api/app.py` | 建 `agent_config_store`（builder 随 `state_store`）→ connect → seed/补齐 15（默认已物化，见 §1）→ `resolver = AgentConfigResolver(list)` → `mcp_manager.server_ids_for = resolver.server_ids_for` → 注入 `AgentNodeRunner(agent_config=…)` |
| CRUD 写后 | `api/app.py` | `_reload_agent_config_resolver()`：重建 resolver + 重接 `mcp_manager.server_ids_for`（运行时节点击热生效） |

---

## 5. 控制面 API（`api/app.py`）

- `GET  /agent-configs` → `list[dict]`：store 行 + `effective_description`（合并后）+ `bound_servers`
  （按 `mcp_server_ids` join `mcp_store.get`；NULL/[] → `[]`（无绑定）；残留引用 → `{id:mid, name:mid, transport:'?'}`）。
- `POST /agent-configs`(201)：只建 **custom**；校验 name 正则 / 撞内置名 400 / role∈{diagnose,fix} / custom 必填非空 prompt；
  `""`→NULL；重复 name → 400 中文。成功后重建 resolver + 重接。返回 `{name, origin:'custom'}`。
- `GET  /agent-configs/{name}`：单条 + **合并有效值**（description/system_prompt/schema 已回退内置，供编辑弹窗提示）
  + `stored`（原始覆盖值，供回填）+ `bound_servers`；404 中文。
- `PUT  /agent-configs/{name}`：完整对象覆盖式更新（builtin/custom 均可）；文本 `""`/不传 → 归 NULL（清覆盖回退）；
  custom 最终 prompt 不能空（400）；origin 不可改。成功重建 + 重接，返回 `{ok:true, name}`。
- `DELETE /agent-configs/{name}`：`origin='builtin'` → 400「内置 agent 不可删除，请用编辑清空覆盖」；custom 删除；404 兜底。
  成功重建 + 重接，返回 `{ok:true}`。
- `GET  /agents`（v1.12 改合并视图）：`resolver.all()` → `[{name, description, tools, stage}]`（tools 仍
  `tools_for_agent(name)` 静态函数工具；custom → `[]`；store 空（未 init）== 纯内置 15，既有 `tests/test_agents_api.py` 不破）。

> 交互负载 `output_schema` 字段（Pydantic `schema` 是 BaseModel 保留字段名，避免冲突 → wire 名 `output_schema`）。

---

## 6. PG / compose 建表

`docker/init/01_schema.sql` 末尾追加 `agent_configs` 的 `CREATE TABLE IF NOT EXISTS`（与应用 `_PG_SCHEMA` 一致）。
sqlite→PG 控制面整库迁移 `_migrate_sqlite_config_to_pg()` 增 `_copy_agent_configs()`（PG 目标表非空即跳过；
源 sqlite 整行搬 PG，name 即 PK，无外键引用）。

---

## 7. 风险 / 备注

- `build_agent` 签名改动已同步调用方（runner + 相关测试）。
- 存了 system_prompt 覆盖后，代码里对该 prompt 的后续修改不再生效（需清空覆盖）—— UI hint 已说明。
- schema 覆盖仅元数据/详情（`extract_json` 不强校验），如实注明。
- `mcp_server_ids` 两态（v1.12.1）：NULL=无 server，`[]` 写入归一 NULL；只有显式勾选绑定的 server
  才是精确子集。运行期 `clients_for_agent` 注入 resolver 后按该子集过滤（空→无 client）；未注入
  resolver 的独立用法（测试）才回退「全部 enabled」。
- 现有绑定过 agent 的 MCP server 记录不受影响（绑定在 agent 侧，server 无 agents 字段）。
