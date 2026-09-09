-- 01_schema.sql — agentflow §8.8 完整表结构（与 agentflow/statestore/postgres.py 的 _SCHEMA 保持一致）
-- 由 docker-entrypoint-initdb.d 在数据卷首次初始化时执行；应用连接时也会 IF NOT EXISTS 幂等建表。
-- 末尾两张控制面配置表（mcp_servers/workflows）与 agentflow/api/mcp_store.py / workflow_store.py
-- 的 _PG_SCHEMA 保持一致：JSON 存 TEXT、布尔存 INTEGER、时间戳存 ISO TEXT（应用读写形状同 sqlite 端）。

CREATE TABLE IF NOT EXISTS workflow_snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    workflow_hash TEXT NOT NULL,
    workflow_yaml TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    workflow_snapshot_id TEXT NOT NULL,
    status              TEXT NOT NULL,
    inputs              JSONB,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nodes (
    run_id    TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    status    TEXT NOT NULL,
    output    JSONB,
    cp        JSONB NOT NULL,
    PRIMARY KEY (run_id, node_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    approvers   JSONB,
    params      JSONB,
    timeout_at  TIMESTAMPTZ,
    approved_by TEXT,
    comment     TEXT,
    UNIQUE (run_id, node_id)
);

CREATE TABLE IF NOT EXISTS node_attempts (
    execution_id          TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL,
    node_id               TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    status                TEXT NOT NULL,
    output                JSONB,
    external_operation_id TEXT,
    error                 TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    decision     TEXT NOT NULL,
    run_id       TEXT,
    node_id      TEXT,
    input_masked TEXT,
    actor        TEXT,
    ts           TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS node_traces (
    id        BIGSERIAL PRIMARY KEY,
    run_id    TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    name      TEXT,
    payload   JSONB NOT NULL,
    ts        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_node_traces_run ON node_traces(run_id, node_id);

-- 控制面配置：MCP Server 配置（SIP「MCP Server 配置」页 CRUD + 运行时 MCPClientManager 读取）
-- 记录只描述 server 本身，不存 agent 绑定（原 agents 列已移除；绑定以 agent 为主表建模）。
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    transport     TEXT NOT NULL,
    config        TEXT NOT NULL,
    is_stateful   INTEGER NOT NULL DEFAULT 1,
    enable_tools  TEXT,
    disable_tools TEXT,
    tools         TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 控制面配置：已保存 workflow 定义（SIP「Workflow Studio」页 CRUD + Bug Solve 页按 id 触发 run）
CREATE TABLE IF NOT EXISTS workflows (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    yaml       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 控制面配置：AgentSpec 配置（SIP「Agent 配置」页 CRUD + 运行时 AgentNodeRunner 读取）
-- 可空列 description/system_prompt/schema_json 存 NULL=未覆盖→回退内置静态默认；
-- seed/init 会把内置行的 description/system_prompt/schema_json 物化为静态默认（DB 为可见快照）。
-- mcp_server_ids（JSON list[str] | NULL）两态（v1.12.1）：NULL/[]=无 MCP server；[mid,…]=精确子集。
CREATE TABLE IF NOT EXISTS agent_configs (
    name            TEXT PRIMARY KEY,
    origin          TEXT NOT NULL DEFAULT 'builtin',
    role            TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'other',
    description     TEXT,
    system_prompt   TEXT,
    schema_json     TEXT,
    mcp_server_ids  TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    reasoning_enabled INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
