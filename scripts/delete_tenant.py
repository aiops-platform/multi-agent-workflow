"""删除一个租户——含 `tenantctl deprovision` 走不通时的补救路径。

用法：
    ./venv/bin/python scripts/delete_tenant.py <tenant>              # 只**体检**，不动手（默认）
    ./venv/bin/python scripts/delete_tenant.py <tenant> --yes        # 真删

为什么不能只靠 `tenantctl deprovision --confirm-delete`：

    它第一步就是 `decrypt(db_ref_enc)` 才知道**要删哪个库**。而
    `AGENTFLOW_SECRET_KEY` 换过（或当初从 `jwt_secret` 派生）时，
    旧租户的 db_ref **解不开** → 抛 ValueError → **整条命令崩在这里**，
    连"把状态置为 deleted"之后的数据清理都做不了。

体检（默认动作）会查清四件事并打印：
    1. 管理库里的租户行（状态 / 隔离级别）
    2. db_ref 能否解密；不能则说明原因，**并给出候选库名**
    3. 按命名约定（`{基础库}-{tenant}`）存在的租户库
    4. 该租户的数据**实际落在哪个库**（扫各租户库的 `runs.tenant_id`）

第 4 步是关键：db_ref 不可信时，**以数据实际位置为准**，而不是以它声称的位置为准。

安全护栏（都是刻意加的）：
    - **默认只体检**，必须 `--yes` 才动手
    - **永不删基础库**（`agentflow`）——那是管理库所在，删了全租户连坐
    - **永不删 `postgres` / `template*`**
    - 动手前打印"将要删什么"，并**要求人工核对**
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.api.management_store import build_management_store, decrypt_db_ref  # noqa: E402
from agentflow.config import get_settings  # noqa: E402
from agentflow.tenants import db_name_of, parse_db_ref  # noqa: E402

#: 任何情况下都不许删的库
PROTECTED = {"postgres", "template0", "template1"}


def _dsn_for(db: str, base_dsn: str) -> str:
    """把基础 DSN 换成指定库（复用 tenants.dsn_with_db 的语义）。"""
    from agentflow.tenants import dsn_with_db

    return dsn_with_db(base_dsn, db)


async def _list_databases(base_dsn: str) -> list[str]:
    from psycopg import AsyncConnection

    conn = await AsyncConnection.connect(_dsn_for(db_name_of(base_dsn), base_dsn))
    try:
        cur = await conn.execute("SELECT datname FROM pg_database ORDER BY 1")
        return [r[0] for r in await cur.fetchall()]
    finally:
        await conn.close()


async def _tenant_data_locations(base_dsn: str, tenant: str, dbs: list[str]) -> dict[str, int]:
    """扫各库的 `runs.tenant_id`，返回 {库名: 该租户的 run 数}。

    **这是"数据实际在哪"的唯一可靠来源**——db_ref 解不开时尤其重要。
    """
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    found: dict[str, int] = {}
    for db in dbs:
        try:
            conn = await AsyncConnection.connect(_dsn_for(db, base_dsn))
            conn.row_factory = dict_row
            try:
                cur = await conn.execute(
                    "SELECT count(*) AS n FROM runs WHERE tenant_id = %s", (tenant,)
                )
                n = (await cur.fetchone())["n"]
                if n:
                    found[db] = n
            finally:
                await conn.close()
        except Exception:  # noqa: BLE001 —— 库不可连/无 runs 表都属正常，跳过
            continue
    return found


async def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    tenant = argv[0]
    do_delete = "--yes" in argv

    settings = get_settings()
    # ⚠️ 必须经 `config.postgres_dsn()`——那是本仓**唯一**的 DSN 归一化点
    #（补 `postgresql://` 前缀）。直接读 `settings.postgres_dsn` 拿到的是
    # `localhost:5432/agentflow?user=…` 这种形态，psycopg 会报
    # `invalid connection option "localhost:/5432/agentflow?user"`（实测踩过）。
    from agentflow.config import postgres_dsn as _normalize

    base_dsn = _normalize(settings)
    base_name = db_name_of(base_dsn)

    print(f"══ 租户 {tenant!r} 删除体检 ══\n")

    # ── 1) 管理库 ──
    mgmt = build_management_store(settings)
    await mgmt.connect()
    try:
        row = await mgmt.get_tenant(tenant)
        if row is None:
            print(f"✗ 管理库里没有租户 {tenant!r}——无需删除")
            return 1
        print(f"1) 管理库租户行")
        print(f"     status={row.get('status')}  isolation={row.get('isolation_level')}")
        print(f"     namespace={row.get('namespace')}  pinned_sha={row.get('pinned_sha')}")

        # ── 2) db_ref 能否解密 ──
        print(f"\n2) db_ref 解密")
        claimed_db = None
        try:
            # ⚠️ 两步，少一步都是错的：
            #   ① `decrypt_db_ref` 返回的是**解出来的原文字符串**（里面是 JSON 文本）
            #   ② 要再 `parse_db_ref` 才拿到 `{"backend", "dsn"}`
            #       —— 与 `statestore/router.py:129` 同一个组合。
            # 早期这里只做了 ① 就按 `ref["dsn"]` 取 → AttributeError 被下面这个 except
            # 吞掉，于是**每一个租户**（包括刚用当前密钥建的、完全健康的）都被报成
            # 「db_ref 解不开：AttributeError」，还把排查方向指向"密钥换过"。
            # 教训：把"真的解不开"和"我自己解析错了"分开报，否则前者永远查不出来。
            ref = parse_db_ref(decrypt_db_ref(row["db_ref_enc"], settings))
            claimed_db = db_name_of(ref["dsn"]) if ref.get("backend") == "postgres" else None
            print(f"     ✓ 可解密  backend={ref.get('backend')}  库名={claimed_db}")
        except Exception as exc:  # noqa: BLE001
            print(f"     ✗ 解不开：{type(exc).__name__}: {exc}")
            print(f"       典型原因：AGENTFLOW_SECRET_KEY 与加密时不同（换过 key，或当初从 jwt_secret 派生）")
            print(f"       ⚠️ `tenantctl deprovision --confirm-delete` 会**崩在这一步**")
            print(f"       ⚠️ 此时**不要相信 db_ref 声称的位置**，以第 4 步「数据实际在哪」为准")

        # ── 3) 候选库 ──
        dbs = await _list_databases(base_dsn)
        conventional = f"{base_name}-{tenant}"
        print(f"\n3) 按命名约定找库")
        print(f"     约定名={conventional}  " + ("存在 ✓" if conventional in dbs else "不存在"))
        if claimed_db and claimed_db != conventional:
            print(f"     db_ref 声称={claimed_db}  " + ("存在 ✓" if claimed_db in dbs else "不存在"))
        # 顺带列出可疑的相邻名字（历史上踩过：非 ASCII 同形字造成近义库名）
        suspicious = [
            d for d in dbs
            if d.startswith(f"{base_name}-") and d != conventional
            and not d.isascii()
        ]
        if suspicious:
            print(f"     ⚠️ 含非 ASCII 的库名（可能是同形字误建）：{suspicious}")

        # ── 4) 数据实际在哪 ──
        tenant_dbs = [d for d in dbs if d.startswith(f"{base_name}-")]
        locations = await _tenant_data_locations(base_dsn, tenant, tenant_dbs)
        print(f"\n4) 数据实际位置（扫各租户库的 runs.tenant_id）")
        if locations:
            for db, n in locations.items():
                print(f"     {db}: {n} 条 run")
        else:
            print(f"     未在任何租户库中找到 {tenant!r} 的 run")

        # ── 计划 ──
        to_drop = sorted(set(locations) | ({conventional} if conventional in dbs else set()))
        print(f"\n══ 计划 ══")
        print(f"  · 管理库：status → deleted（幂等）")
        if to_drop:
            for db in to_drop:
                print(f"  · 删库 {db}  （{locations.get(db, 0)} 条 run）")
        else:
            print(f"  · 无库可删（该租户没有独立库——可能是旧「共享 DSN」方案开通的）")

        guarded = [d for d in to_drop if d in PROTECTED or d == base_name]
        if guarded:
            print(f"\n✗ 拒绝继续：这些是受保护的库名 {guarded}（基础库/系统库，删了会连坐）")
            return 1

        if not do_delete:
            print(f"\n（体检模式，未改动任何东西。确认无误后加 --yes 执行）")
            return 0

        # ── 执行 ──
        print(f"\n══ 执行 ══")
        await mgmt.update_tenant(tenant, status="deleted")
        print(f"  ✓ status=deleted")

        from agentflow.tenants import drop_tenant_database

        for db in to_drop:
            ref = {"backend": "postgres", "dsn": _dsn_for(db, base_dsn)}
            ok = await drop_tenant_database(ref, settings)
            print(f"  {'✓ 已删除' if ok else '− 跳过（拒绝删基础库或已不存在）'} {db}")
        return 0
    finally:
        await mgmt.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
