"""ManagementStore：管理库（design-v5.3 §5.2，平台唯一跨租户视图）。

存租户注册表（tenants）与租户库 schema 版本（schema_versions）。**管理库是皇冠
明珠**：db_ref（租户库连接引用）必须 Fernet 加密落库（§12 风险 1），任何 API 不回显。

- 密钥：``AGENTFLOW_SECRET_KEY``（32 字节 urlsafe base64）；缺省从 ``jwt_secret``
  派生（SHA256 → urlsafe base64，启动告警）；两者皆空（dev）→ 明文存储（前缀区分）。
- 密文格式：``enc:<base64>``（加密） / ``plain:<value>``（dev 明文），读时按前缀解。
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import Settings, postgres_dsn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id           TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    isolation_level     TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    workers             INTEGER NOT NULL,
    max_concurrent_runs INTEGER NOT NULL,
    approvers           TEXT,
    db_ref_enc          TEXT NOT NULL,
    code_branch         TEXT NOT NULL,
    pinned_sha          TEXT,
    image_tag           TEXT,
    deployed_at         TEXT,
    main_base_sha       TEXT,
    created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_versions (
    tenant_id  TEXT NOT NULL,
    pinned_sha TEXT NOT NULL,
    version    TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, pinned_sha)
);
"""


# ----------------------------------------------------------------------
# db_ref 加密（§5.3 凭证保护）
# ----------------------------------------------------------------------
def derive_secret_key(settings: Settings) -> tuple[bytes, bool]:
    """返回 (Fernet key, 是否派生)。显式 AGENTFLOW_SECRET_KEY 优先；缺省从 jwt_secret 派生。"""
    if settings.secret_key:
        return settings.secret_key.encode(), False
    if settings.jwt_secret:
        derived = hashlib.sha256(settings.jwt_secret.encode()).digest()
        return base64.urlsafe_b64encode(derived), True
    return b"", False


def encrypt_db_ref(value: str, settings: Settings) -> str:
    """db_ref → ``enc:<b64>`` / ``plain:<value>``（dev 无密钥时明文，前缀区分）。"""
    key, _derived = derive_secret_key(settings)
    if not key:
        return f"plain:{value}"
    from cryptography.fernet import Fernet

    token = Fernet(key).encrypt(value.encode()).decode()
    return f"enc:{token}"


def decrypt_db_ref(stored: str, settings: Settings) -> str:
    if stored.startswith("plain:"):
        return stored[len("plain:"):]
    if stored.startswith("enc:"):
        key, _derived = derive_secret_key(settings)
        if not key:
            raise ValueError("db_ref 已加密但未配置 AGENTFLOW_SECRET_KEY/AGENTFLOW_JWT_SECRET")
        from cryptography.fernet import Fernet, InvalidToken

        try:
            return Fernet(key).decrypt(stored[len("enc:"):].encode()).decode()
        except InvalidToken as exc:  # 密钥不匹配（换密钥/换环境）
            raise ValueError(f"db_ref 解密失败（密钥不匹配？）: {exc}") from exc
    return stored  # 兼容历史明文行


# ----------------------------------------------------------------------
# Store（sqlite + PG 双实现，模式对齐 workflow_store）
# ----------------------------------------------------------------------
class ManagementStore:
    """管理库 sqlite 实现（aiosqlite，惰性连接）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "ManagementStore 未 connect()"
        return self._conn

    @staticmethod
    def _row(d: dict) -> dict:
        d["approvers"] = json.loads(d["approvers"] or "{}")
        return d

    # ---- tenants ----
    async def upsert_tenant(self, row: dict) -> None:
        """按 tenant_id 整行 upsert（provisioner/重建 的幂等写入点）。"""
        await self.connect()
        await self._c.execute(
            "INSERT INTO tenants(tenant_id, status, isolation_level, namespace, workers,"
            " max_concurrent_runs, approvers, db_ref_enc, code_branch, pinned_sha,"
            " image_tag, deployed_at, main_base_sha, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id) DO UPDATE SET"
            " status=excluded.status, isolation_level=excluded.isolation_level,"
            " namespace=excluded.namespace, workers=excluded.workers,"
            " max_concurrent_runs=excluded.max_concurrent_runs, approvers=excluded.approvers,"
            " db_ref_enc=excluded.db_ref_enc, code_branch=excluded.code_branch,"
            " pinned_sha=excluded.pinned_sha, image_tag=excluded.image_tag,"
            " deployed_at=excluded.deployed_at, main_base_sha=excluded.main_base_sha",
            (
                row["tenant_id"], row.get("status", "active"),
                row.get("isolation_level", "standard"), row["namespace"],
                int(row.get("workers", 1)), int(row.get("max_concurrent_runs", 10)),
                json.dumps(row.get("approvers") or {}),
                row["db_ref_enc"], row.get("code_branch", "main"),
                row.get("pinned_sha"), row.get("image_tag"), row.get("deployed_at"),
                row.get("main_base_sha"),
                row.get("created_at") or datetime.now(UTC).isoformat(),
            ),
        )
        await self._c.commit()

    async def get_tenant(self, tenant_id: str) -> dict | None:
        await self.connect()
        cur = await self._c.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,))
        row = await cur.fetchone()
        return self._row(dict(row)) if row else None

    async def list_tenants(self, status: str | None = None) -> list[dict]:
        await self.connect()
        if status:
            cur = await self._c.execute(
                "SELECT * FROM tenants WHERE status=? ORDER BY tenant_id", (status,)
            )
        else:
            cur = await self._c.execute("SELECT * FROM tenants ORDER BY tenant_id")
        rows = await cur.fetchall()
        return [self._row(dict(r)) for r in rows]

    async def update_tenant(self, tenant_id: str, **fields: Any) -> bool:
        """部分更新（deploy pin SHA / status 流转等）。"""
        await self.connect()
        if not fields:
            return True
        cols, vals = [], []
        for k, v in fields.items():
            if k == "approvers":
                v = json.dumps(v or {})
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(tenant_id)
        cur = await self._c.execute(
            f"UPDATE tenants SET {', '.join(cols)} WHERE tenant_id=?", vals
        )
        await self._c.commit()
        return cur.rowcount == 1

    # ---- schema_versions ----
    async def set_schema_version(self, tenant_id: str, pinned_sha: str, version: str) -> None:
        await self.connect()
        await self._c.execute(
            "INSERT INTO schema_versions(tenant_id, pinned_sha, version, applied_at)"
            " VALUES(?,?,?,?) ON CONFLICT(tenant_id, pinned_sha) DO UPDATE SET"
            " version=excluded.version, applied_at=excluded.applied_at",
            (tenant_id, pinned_sha, version, datetime.now(UTC).isoformat()),
        )
        await self._c.commit()

    async def get_schema_version(self, tenant_id: str, pinned_sha: str) -> str | None:
        await self.connect()
        cur = await self._c.execute(
            "SELECT version FROM schema_versions WHERE tenant_id=? AND pinned_sha=?",
            (tenant_id, pinned_sha),
        )
        row = await cur.fetchone()
        return row["version"] if row else None


class PgManagementStore:
    """管理库 PostgreSQL 实现（psycopg3 async，方法面与 sqlite 版一致）。"""

    _PG_SCHEMA = _SCHEMA

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        self._conn = await AsyncConnection.connect(self._dsn)
        self._conn.row_factory = dict_row
        await self._conn.execute(self._PG_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self):
        assert self._conn is not None, "PgManagementStore 未 connect()"
        return self._conn

    @staticmethod
    def _row(d: dict) -> dict:
        if isinstance(d.get("approvers"), str):
            d["approvers"] = json.loads(d["approvers"] or "{}")
        return d

    async def upsert_tenant(self, row: dict) -> None:
        await self.connect()
        await self._c.execute(
            "INSERT INTO tenants(tenant_id, status, isolation_level, namespace, workers,"
            " max_concurrent_runs, approvers, db_ref_enc, code_branch, pinned_sha,"
            " image_tag, deployed_at, main_base_sha, created_at)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT(tenant_id) DO UPDATE SET"
            " status=EXCLUDED.status, isolation_level=EXCLUDED.isolation_level,"
            " namespace=EXCLUDED.namespace, workers=EXCLUDED.workers,"
            " max_concurrent_runs=EXCLUDED.max_concurrent_runs, approvers=EXCLUDED.approvers,"
            " db_ref_enc=EXCLUDED.db_ref_enc, code_branch=EXCLUDED.code_branch,"
            " pinned_sha=EXCLUDED.pinned_sha, image_tag=EXCLUDED.image_tag,"
            " deployed_at=EXCLUDED.deployed_at, main_base_sha=EXCLUDED.main_base_sha",
            (
                row["tenant_id"], row.get("status", "active"),
                row.get("isolation_level", "standard"), row["namespace"],
                int(row.get("workers", 1)), int(row.get("max_concurrent_runs", 10)),
                json.dumps(row.get("approvers") or {}),
                row["db_ref_enc"], row.get("code_branch", "main"),
                row.get("pinned_sha"), row.get("image_tag"), row.get("deployed_at"),
                row.get("main_base_sha"),
                row.get("created_at") or datetime.now(UTC).isoformat(),
            ),
        )
        await self._c.commit()

    async def get_tenant(self, tenant_id: str) -> dict | None:
        await self.connect()
        cur = await self._c.execute("SELECT * FROM tenants WHERE tenant_id=%s", (tenant_id,))
        row = await cur.fetchone()
        return self._row(dict(row)) if row else None

    async def list_tenants(self, status: str | None = None) -> list[dict]:
        await self.connect()
        if status:
            cur = await self._c.execute(
                "SELECT * FROM tenants WHERE status=%s ORDER BY tenant_id", (status,)
            )
        else:
            cur = await self._c.execute("SELECT * FROM tenants ORDER BY tenant_id")
        rows = await cur.fetchall()
        return [self._row(dict(r)) for r in rows]

    async def update_tenant(self, tenant_id: str, **fields: Any) -> bool:
        await self.connect()
        if not fields:
            return True
        cols, vals = [], []
        for k, v in fields.items():
            if k == "approvers":
                v = json.dumps(v or {})
            cols.append(f"{k}=%s")
            vals.append(v)
        vals.append(tenant_id)
        cur = await self._c.execute(
            f"UPDATE tenants SET {', '.join(cols)} WHERE tenant_id=%s", vals
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def set_schema_version(self, tenant_id: str, pinned_sha: str, version: str) -> None:
        await self.connect()
        await self._c.execute(
            "INSERT INTO schema_versions(tenant_id, pinned_sha, version, applied_at)"
            " VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id, pinned_sha) DO UPDATE SET"
            " version=EXCLUDED.version, applied_at=EXCLUDED.applied_at",
            (tenant_id, pinned_sha, version, datetime.now(UTC).isoformat()),
        )
        await self._c.commit()

    async def get_schema_version(self, tenant_id: str, pinned_sha: str) -> str | None:
        await self.connect()
        cur = await self._c.execute(
            "SELECT version FROM schema_versions WHERE tenant_id=%s AND pinned_sha=%s",
            (tenant_id, pinned_sha),
        )
        row = await cur.fetchone()
        return row["version"] if row else None


def build_management_store(settings: Settings) -> ManagementStore | PgManagementStore:
    """按 state_store 选择管理库后端（管理库与运行期同后端策略）。"""
    if settings.state_store == "postgres":
        return PgManagementStore(postgres_dsn(settings))
    return ManagementStore(Path(settings.state_db_path).parent / "management.db")
