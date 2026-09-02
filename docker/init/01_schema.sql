-- 01_schema.sql — agentflow §8.8 完整表结构（与 agentflow/statestore/postgres.py 的 _SCHEMA 保持一致）
-- 由 docker-entrypoint-initdb.d 在数据卷首次初始化时执行；应用连接时也会 IF NOT EXISTS 幂等建表。

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
