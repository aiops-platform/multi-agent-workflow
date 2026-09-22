"""TicketStore：运维工单的持久化（供 SIP「Ticket Inbox」页面使用）。

**为什么需要它**：控制面此前在 run 维度是「一次性」的 —— ``ticket`` 只是 ``POST /run``
body 的一个字段，被拍平成 run 的 ``inputs`` 存一次，此后再无任何 API 能读回它。
于是「这条 run 来自哪个工单」查不回来，前端也没法列出待处理的工单。

本表补上这一层：工单独立落库，记录它发起过哪些 run（``run_ids``），
使「工单 → run → 诊断/修复结果」这条链可查。

**租户隔离**：所有方法都带 ``tenant_id`` 并强制过滤（跨租户查不到，不是靠调用方自觉）。
注意 ``WorkflowStore`` 没有 tenant 列（历史遗留，workflow 定义目前在控制面库全局共享），
本表**刻意不跟随**那个模式 —— 工单天然是租户数据。

与 ``WorkflowStore`` 同款形态：SQLite（aiosqlite，惰性幂等 connect）+
PostgreSQL（psycopg3 async）两实现，方法面与返回形状一致，供 API 层无感切换。

**JSON 列一律用 TEXT**（不用 jsonb）：一是两端解码一致，二是避开 psycopg 对 jsonb
自动解析成 dict、而 sqlite 返回 str 的不对称（本仓已在别处踩过这个坑）。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import postgres_dsn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    number     TEXT,
    title      TEXT NOT NULL,
    service    TEXT,
    namespace  TEXT,
    severity   TEXT,
    status     TEXT NOT NULL,
    inputs     TEXT NOT NULL,
    run_ids    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_ref TEXT
);
"""

#: 索引与取号表——**必须与建表分开执行**：`uq_tickets_source_ref` 引用 `source_ref` 列，
#: 而对**已存在**的 tickets 表要先 `ALTER TABLE` 补列（见 `_ensure_columns`）。
#: 揉进一个 `executescript` 的话，旧库会在建索引那一步直接报 "no such column"。
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tickets_tenant ON tickets(tenant_id, created_at DESC);
-- source_ref = 「这条工单是为哪条数据建的」（目前是问题单号）。唯一索引就是
-- `create_once` 的并发兜底：两个进程同时建，只有一个插得进去，另一个撞冲突后回读。
-- **非部分索引**：SQL 里 NULL 互不相等，所以历史行（source_ref 为空）之间不会互相冲突。
CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_source_ref ON tickets(tenant_id, source_ref);
-- ticket_seq：工单号（INC-YYYYMMDD-NNNN）按日期原子取号。照抄问题单取号的成熟做法
-- （aiops-apm 的 record_seq）：INSERT ... ON CONFLICT DO UPDATE ... RETURNING，免序列对象。
CREATE TABLE IF NOT EXISTS ticket_seq (
    seq_date  TEXT    NOT NULL PRIMARY KEY,
    next_seq  INTEGER NOT NULL DEFAULT 1
);
"""

# 工单状态。与 run 状态分开 —— 一个工单可以发起多次 run，工单自身的进度
# 是它所有 run 的汇总，不等于任何单个 run 的状态。
TICKET_NEW = "new"
TICKET_RUNNING = "running"
TICKET_RESOLVED = "resolved"
TICKET_FAILED = "failed"

#: 列清单。**顺序即 INSERT 的位置参数顺序**——新增列一律**追加在末尾**，
#: 免得改动前面 12 个位置参数的对应关系（那是最容易静默错位的地方）。
_COLS = (
    "id, tenant_id, number, title, service, namespace, severity,"
    " status, inputs, run_ids, created_at, updated_at, source_ref"
)

#: `_COLS` 的占位符（sqlite 用 `?`、PG 用 `%s`）；两处 INSERT 共用，防手抄错个数。
_PLACEHOLDERS_SQLITE = ",".join(["?"] * 13)
_PLACEHOLDERS_PG = ",".join(["%s"] * 13)


def _row_to_ticket(row: Any) -> dict[str, Any]:
    """行 → dict；统一反序列化两个 JSON TEXT 列。"""
    d = dict(row)
    d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
    d["run_ids"] = json.loads(d["run_ids"]) if d.get("run_ids") else []
    return d


class TicketStore:
    """工单的轻量 CRUD 存储（aiosqlite，惰性连接）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """幂等连接；首次调用时建表。"""
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._ensure_columns()  # 旧库补列，必须早于建索引
        await self._conn.executescript(_SCHEMA_INDEXES)
        await self._conn.commit()

    async def _ensure_columns(self) -> None:
        """给**已存在**的 tickets 表补 `source_ref` 列。

        `CREATE TABLE IF NOT EXISTS` 对已建好的表**什么也不做** —— 所以已经开通的租户库
        （otr / local…）不会自动拿到新列，而 `CREATE UNIQUE INDEX` 会直接报
        "no such column"。SQLite 的 `ALTER TABLE` 没有 `IF NOT EXISTS`，只能先问 PRAGMA。
        """
        cur = await self._c.execute("PRAGMA table_info(tickets)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "source_ref" not in cols:
            await self._c.execute("ALTER TABLE tickets ADD COLUMN source_ref TEXT")

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "TicketStore 未 connect()"
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def create(
        self,
        tenant_id: str,
        *,
        title: str,
        inputs: dict,
        number: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str = TICKET_NEW,
        source_ref: str | None = None,
    ) -> str:
        """建工单，返回 id（12 位 hex）。

        **无查重**——重复调用就重复建单。要"同一条数据只建一张"用 :meth:`create_once`。
        """
        await self.connect()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        await self._c.execute(
            f"INSERT INTO tickets({_COLS}) VALUES({_PLACEHOLDERS_SQLITE})",
            (
                tid, tenant_id, number, title, service, namespace, severity,
                status, json.dumps(inputs, ensure_ascii=False), "[]", now, now,
                source_ref,
            ),
        )
        await self._c.commit()
        return tid

    async def get_by_source_ref(self, tenant_id: str, source_ref: str) -> dict[str, Any] | None:
        """按「这条工单是为哪条数据建的」查（`create_once` 的判据）。"""
        await self.connect()
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE tenant_id=? AND source_ref=?", (tenant_id, source_ref)
        )
        row = await cur.fetchone()
        return None if row is None else _row_to_ticket(row)

    async def create_once(
        self,
        tenant_id: str,
        *,
        source_ref: str,
        title: str,
        inputs: dict,
        number: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str = TICKET_NEW,
    ) -> tuple[dict[str, Any], bool]:
        """**按 `source_ref` 查重后建单**：已有 → 返回那一张（`created=False`）；否则新建（`True`）。

        幂等放在**接口层**而不是靠引擎的 `external_operation_id`：后者只活在 attempt 账本里，
        管得住"同 run 重放"，管不住"同一条数据被两个 run / 两次手工重跑各建一张"。
        这里的不变量更强——**一条数据永远只有一张工单**，跨进程、跨重放都成立。

        并发兜底是 `uq_tickets_source_ref` 唯一索引：两个进程同时走到 INSERT，只有一个成，
        另一个撞冲突（`DO NOTHING`）后回读，拿到的还是同一张。
        """
        await self.connect()
        existing = await self.get_by_source_ref(tenant_id, source_ref)
        if existing is not None:
            return existing, False

        if number is None:
            # **取号放在查重之后**：幂等命中时不该白白烧掉一个号（号是要给人看的，跳号像丢号）
            number = await self.next_number()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        await self._c.execute(
            f"INSERT INTO tickets({_COLS}) VALUES({_PLACEHOLDERS_SQLITE}) "
            "ON CONFLICT(tenant_id, source_ref) DO NOTHING",
            (
                tid, tenant_id, number, title, service, namespace, severity,
                status, json.dumps(inputs, ensure_ascii=False), "[]", now, now,
                source_ref,
            ),
        )
        await self._c.commit()
        row = await self.get_by_source_ref(tenant_id, source_ref)
        if row is None:  # 插了又查不到：只可能是并发把我们的行删了，不该发生
            raise RuntimeError(f"工单建后回读失败: tenant={tenant_id} source_ref={source_ref}")
        return row, row["id"] == tid

    async def next_number(self, *, now: datetime | None = None) -> str:
        """工单号 ``INC-YYYYMMDD-NNNN``（按日期原子自增）。

        与问题单号（``PR-``，在 aiops-apm 侧）**格式同族但各自计数**：那张表按日期分段，
        两串号共用一张表会互相跳号，看起来像丢号。
        """
        await self.connect()
        seq_date = (now or datetime.now(UTC)).strftime("%Y%m%d")
        cur = await self._c.execute(
            "INSERT INTO ticket_seq(seq_date, next_seq) VALUES(?, 1) "
            "ON CONFLICT(seq_date) DO UPDATE SET next_seq = ticket_seq.next_seq + 1 "
            "RETURNING next_seq",
            (seq_date,),
        )
        row = await cur.fetchone()
        await self._c.commit()
        return f"INC-{seq_date}-{int(row[0] if row else 1):04d}"

    async def list(
        self, tenant_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """按创建时间倒序列出该租户的工单。"""
        await self.connect()
        where = ["tenant_id = ?"]
        vals: list[Any] = [tenant_id]
        if status is not None:
            where.append("status = ?")
            vals.append(status)
        vals.extend([limit, offset])
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            vals,
        )
        return [_row_to_ticket(r) for r in await cur.fetchall()]

    async def get(self, tenant_id: str, tid: str) -> dict[str, Any] | None:
        """取单条（**强制租户过滤** —— 跨租户返回 None，不是 403，避免泄漏存在性）。"""
        await self.connect()
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE id=? AND tenant_id=?", (tid, tenant_id)
        )
        row = await cur.fetchone()
        return _row_to_ticket(row) if row else None

    async def attach_run(self, tenant_id: str, tid: str, run_id: str) -> bool:
        """把一次 run 挂到工单上（幂等：重复挂同一个 run_id 不重复追加）。"""
        ticket = await self.get(tenant_id, tid)
        if ticket is None:
            return False
        if run_id in ticket["run_ids"]:
            return True
        run_ids = [*ticket["run_ids"], run_id]
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET run_ids=?, updated_at=? WHERE id=? AND tenant_id=?",
            (json.dumps(run_ids), datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def set_status(self, tenant_id: str, tid: str, status: str) -> bool:
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
            (status, datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1


# ----------------------------------------------------------------------
# PostgreSQL 后端（state_store=postgres 时由 build_ticket_store 选择）
# ----------------------------------------------------------------------
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    number     TEXT,
    title      TEXT NOT NULL,
    service    TEXT,
    namespace  TEXT,
    severity   TEXT,
    status     TEXT NOT NULL,
    inputs     TEXT NOT NULL,
    run_ids    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_ref TEXT
);
"""

#: 见 SQLite 侧 `_SCHEMA_INDEXES` 的说明：索引与建表分开，因为 `source_ref` 列要
#: 先给**已存在**的表补上（PG 用 `ADD COLUMN IF NOT EXISTS`，不用像 SQLite 那样问 PRAGMA）。
_PG_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tickets_tenant ON tickets(tenant_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_source_ref ON tickets(tenant_id, source_ref);
CREATE TABLE IF NOT EXISTS ticket_seq (
    seq_date  TEXT    NOT NULL PRIMARY KEY,
    next_seq  INTEGER NOT NULL DEFAULT 1
);
"""


class PgTicketStore:
    """TicketStore 的 PostgreSQL 实现（psycopg3 async，单连接，幂等 connect）。"""

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
        await self._conn.execute(_PG_SCHEMA)
        # 旧库补列，必须早于建索引（与 SQLite 侧同因）；PG 原生支持 IF NOT EXISTS
        await self._conn.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS source_ref TEXT")
        await self._conn.execute(_PG_SCHEMA_INDEXES)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self):
        assert self._conn is not None, "PgTicketStore 未 connect()"
        return self._conn

    async def create(
        self,
        tenant_id: str,
        *,
        title: str,
        inputs: dict,
        number: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str = TICKET_NEW,
        source_ref: str | None = None,
    ) -> str:
        await self.connect()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        await self._c.execute(
            f"INSERT INTO tickets({_COLS}) VALUES({_PLACEHOLDERS_PG})",
            (
                tid, tenant_id, number, title, service, namespace, severity,
                status, json.dumps(inputs, ensure_ascii=False), "[]", now, now,
                source_ref,
            ),
        )
        await self._c.commit()
        return tid

    async def get_by_source_ref(self, tenant_id: str, source_ref: str) -> dict[str, Any] | None:
        await self.connect()
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE tenant_id=%s AND source_ref=%s",
            (tenant_id, source_ref),
        )
        row = await cur.fetchone()
        return None if row is None else _row_to_ticket(row)

    async def create_once(
        self,
        tenant_id: str,
        *,
        source_ref: str,
        title: str,
        inputs: dict,
        number: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        severity: str | None = None,
        status: str = TICKET_NEW,
    ) -> tuple[dict[str, Any], bool]:
        """见 SQLite 侧同名方法的说明（语义逐字相同）。"""
        await self.connect()
        existing = await self.get_by_source_ref(tenant_id, source_ref)
        if existing is not None:
            return existing, False

        if number is None:
            # **取号放在查重之后**：幂等命中时不该白白烧掉一个号（号是要给人看的，跳号像丢号）
            number = await self.next_number()
        tid = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        await self._c.execute(
            f"INSERT INTO tickets({_COLS}) VALUES({_PLACEHOLDERS_PG}) "
            "ON CONFLICT(tenant_id, source_ref) DO NOTHING",
            (
                tid, tenant_id, number, title, service, namespace, severity,
                status, json.dumps(inputs, ensure_ascii=False), "[]", now, now,
                source_ref,
            ),
        )
        await self._c.commit()
        row = await self.get_by_source_ref(tenant_id, source_ref)
        if row is None:
            raise RuntimeError(f"工单建后回读失败: tenant={tenant_id} source_ref={source_ref}")
        return row, row["id"] == tid

    async def next_number(self, *, now: datetime | None = None) -> str:
        """见 SQLite 侧同名方法的说明。"""
        await self.connect()
        seq_date = (now or datetime.now(UTC)).strftime("%Y%m%d")
        cur = await self._c.execute(
            "INSERT INTO ticket_seq(seq_date, next_seq) VALUES(%s, 1) "
            "ON CONFLICT(seq_date) DO UPDATE SET next_seq = ticket_seq.next_seq + 1 "
            "RETURNING next_seq",
            (seq_date,),
        )
        row = await cur.fetchone()
        await self._c.commit()
        return f"INC-{seq_date}-{int((row or {}).get('next_seq') or 1):04d}"

    async def list(
        self, tenant_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        await self.connect()
        where = ["tenant_id = %s"]
        vals: list[Any] = [tenant_id]
        if status is not None:
            where.append("status = %s")
            vals.append(status)
        vals.extend([limit, offset])
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            vals,
        )
        return [_row_to_ticket(r) for r in await cur.fetchall()]

    async def get(self, tenant_id: str, tid: str) -> dict[str, Any] | None:
        await self.connect()
        cur = await self._c.execute(
            f"SELECT {_COLS} FROM tickets WHERE id=%s AND tenant_id=%s", (tid, tenant_id)
        )
        row = await cur.fetchone()
        return _row_to_ticket(row) if row else None

    async def attach_run(self, tenant_id: str, tid: str, run_id: str) -> bool:
        ticket = await self.get(tenant_id, tid)
        if ticket is None:
            return False
        if run_id in ticket["run_ids"]:
            return True
        run_ids = [*ticket["run_ids"], run_id]
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET run_ids=%s, updated_at=%s WHERE id=%s AND tenant_id=%s",
            (json.dumps(run_ids), datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1

    async def set_status(self, tenant_id: str, tid: str, status: str) -> bool:
        await self.connect()
        cur = await self._c.execute(
            "UPDATE tickets SET status=%s, updated_at=%s WHERE id=%s AND tenant_id=%s",
            (status, datetime.now(UTC).isoformat(), tid, tenant_id),
        )
        await self._c.commit()
        return cur.rowcount == 1


def build_ticket_store(settings):
    """按 state_store 选择控制面 ticket 存储后端（对齐 build_workflow_store）。

    > 单租户/未 init 时的回退用。**多租户路径必须走 ``TenantStores.ticket``**
    > （router 按租户库构造）—— 早期只用了这个全局构造器，导致工单永远落在
    > 共享库里、不随租户隔离（见 ``build_ticket_store_at`` 的说明）。
    """
    if settings.state_store == "postgres":
        return PgTicketStore(postgres_dsn(settings))
    return TicketStore(settings.state_db_path)


def build_ticket_store_at(backend: str, dsn: str | None, settings=None):
    """按租户库的 backend/dsn 构造（router 用；对齐 build_mcp_store_at 等）。

    **这是租户隔离的关键一环**：工单存储必须和其它配置表一样跟着租户库走。
    早期 `ticket_store` 只作为模块全局存在、不在 `TenantStores` bundle 里，
    于是所有租户的工单都写进共享库 —— 实测 `otr` 的 run 落在 `agentflow-otr`，
    而同一租户的 ticket 落到了 `agentflow`。表现上"能用"（查询仍按 tenant_id 过滤），
    但破坏了"每租户一个库"的物理隔离模型。
    """
    if backend == "postgres":
        return PgTicketStore(dsn)
    assert settings is not None
    return build_ticket_store(settings)


def _ticket_fields_from_params(params: dict) -> dict[str, Any]:
    """`kind: ticket` 节点的 params → 工单字段。

    params 由 workflow 的 YAML 声明（`$.inputs.bug_report` 等），所以在**图上**就能看到
    这张工单由什么组成；这里只做形状映射，不做业务判断。
    """
    src_bug = params.get("bug_report") if isinstance(params.get("bug_report"), dict) else {}
    # **浅拷贝**：下面要往 bug_report 里塞诊断结论，不能就地改调用方的 params。
    bug = dict(src_bug)
    ci = bug.get("cmdb_ci") if isinstance(bug.get("cmdb_ci"), dict) else {}
    # 诊断结论随工单走：拿到工单的人不必回平台翻诊断。
    # ⚠️ 落点必须是 **`bug_report` 里面**，与 APM 侧 `_agentflow_create_ticket` 的
    #    `bug_report["diagnosis"] = digest` 同一位置：SIP 工单详情页只渲染
    #    `inputs.bug_report` 那一段 JSON（「工单内容（诊断的输入 bug_report）」那个 `<pre>`），
    #    写在 `inputs` 顶层 = **写了没人看得见**。2026-09-22 实测：INC-20260922-0001 的
    #    `inputs.diagnosis` 里 rca/plan 齐全，页面上一个字都没有（只有 APM 建的老单看得见，
    #    因为老路径正好写在 bug_report 里）。
    diag = {k: params[k] for k in ("rca", "plan") if params.get(k)}
    if diag:
        bug["diagnosis"] = diag
    inputs: dict[str, Any] = {"bug_report": bug}
    # 时间窗：None 时**不写键**（与 POST /tickets 的 _ticket_inputs 同约定——
    # workflow 的 window_start/end 声明为 required，缺键报错比传 null 更直白）
    for k in ("window_start", "window_end"):
        if params.get(k):
            inputs[k] = params[k]
    return {
        "title": str(bug.get("short_description") or params.get("title") or "").strip()[:200]
        or "未命名工单",
        "inputs": inputs,
        "service": bug.get("service") or ci.get("name"),
        "namespace": params.get("namespace") or ci.get("namespace"),
        "severity": params.get("severity") or bug.get("severity"),
    }


async def create_from_node_params(store: Any, tenant_id: str, params: dict) -> dict[str, Any]:
    """`kind: ticket` 节点的建单实现（组合根把 `store` 绑好再注入 executor）。

    **幂等口径 = `source_ref`（问题单号）**：同一条数据重复触发只会拿到同一张工单，
    见 :meth:`create_once`。缺 `source_ref` 直接报错——没有它就没法保证"一条数据一张单"，
    宁可失败也不要建出一张无法去重的单。
    """
    source_ref = str(params.get("source_ref") or "").strip()
    if not source_ref:
        raise ValueError(
            "建单节点缺少 source_ref（问题单号）——它是幂等判据，"
            "没有它就无法保证「一条数据只建一张工单」"
        )
    row, created = await store.create_once(
        tenant_id, source_ref=source_ref, **_ticket_fields_from_params(params)
    )
    return {
        "ticket_id": row["id"],
        "ticket_number": row.get("number"),
        "title": row.get("title"),
        "service": row.get("service"),
        "severity": row.get("severity"),
        "created": created,  # False = 命中已有工单（幂等复用），不是失败
        "source_ref": source_ref,
    }
