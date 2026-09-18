"""DSN 口令打码（`docs/TODO.md` §24 残留）。

DSN 里带明文口令，打进终端回滚 / CI 日志 / 截图就收不回来。`tenantctl` 需要
"建了哪个库"这个信息（host / port / 库名 / 用户名都有用），**只有口令没有**。
这与 `CLAUDE.md` 既有的「任何 API 不回显 DSN」是同一条原则。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentflow.tenants import mask_dsn

TENANTCTL = Path(__file__).resolve().parent.parent / "agentflow" / "tenantctl.py"


# ======================================================================
# mask_dsn 本身
# ======================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # netloc 形态（`--db-dsn` / K8s manifest 走这条）
        (
            "postgresql://agentflow:agentflow@10.89.0.2:5432/agentflow-otr",
            "postgresql://agentflow:***@10.89.0.2:5432/agentflow-otr",
        ),
        # query 形态（`postgres_dsn(settings)` 的产出）——口令不在 netloc 而在 query
        (
            "postgresql://host:5432/agentflow?user=u&password=p",
            "postgresql://host:5432/agentflow?user=u&password=***",
        ),
        # 口令是 query 的第一个参数
        (
            "postgresql://host:5432/agentflow?password=p&user=u",
            "postgresql://host:5432/agentflow?password=***&user=u",
        ),
        # 大小写不敏感，且不能吞掉后续参数（`&` 必须保留）
        (
            "postgresql://host:5432/agentflow?user=u&PASSWORD=p&sslmode=require",
            "postgresql://host:5432/agentflow?user=u&PASSWORD=***&sslmode=require",
        ),
        # `pwd` 别名
        (
            "postgresql://host:5432/agentflow?user=u&pwd=p",
            "postgresql://host:5432/agentflow?user=u&pwd=***",
        ),
        # 口令里含 `@` / `:` —— 必须按**最后一个** `@` 切 userinfo，否则会把口令
        # 的一部分当成 host 留下（用 partition 而非 rpartition 就会踩）
        (
            "postgresql://user:p@ss:word@host:5432/db",
            "postgresql://user:***@host:5432/db",
        ),
        # 无口令 → 原样，**不该凭空造一个 `user:***`**
        ("postgresql://user@host:5432/db", "postgresql://user@host:5432/db"),
        # 空口令 → 原样（`password=` 本就是空的，打码成 *** 反而是误导）
        ("postgresql://host:5432/db?user=u&password=", "postgresql://host:5432/db?user=u&password="),
        # 非 URL（sqlite 路径）→ 原样，不报错
        ("data/tenants/otr.db", "data/tenants/otr.db"),
    ],
)
def test_mask_dsn_masks_password_and_keeps_everything_else(raw: str, expected: str) -> None:
    assert mask_dsn(raw) == expected


def test_mask_dsn_keeps_host_db_and_user_readable() -> None:
    """打码**只**针对口令——其余信息是排查要用到的，不能一起糊掉。"""
    out = mask_dsn("postgresql://agentflow:agentflow@10.89.0.2:5432/agentflow-otr")
    for keep in ("10.89.0.2", "5432", "agentflow-otr", "agentflow"):
        assert keep in out


@pytest.mark.parametrize("empty", [None, ""])
def test_mask_dsn_empty_passthrough(empty) -> None:
    """空值原样（`None` → `"None"`）：调用方靠它区分"没有 DSN"，别在这里改语义。"""
    assert mask_dsn(empty) == str(empty)


# ======================================================================
# tenantctl 不许回显未打码的 DSN
# ======================================================================
def _print_segments(src: str) -> list[tuple[int, str]]:
    """所有 `print(...)` 调用的 (行号, 源码片段)。"""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            seg = ast.get_source_segment(src, node) or ""
            out.append((node.lineno, seg))
    return out


def test_tenantctl_never_prints_unmasked_dsn() -> None:
    """AST 守卫：凡是把 ``dsn`` **当作值**取出来的 ``print``，都必须过 ``mask_dsn``。

    为什么不靠人眼：这几处相隔几百行，改的时候很容易只记得改一处——
    本项缺陷就是这么来的（同一份 DSN 在 5 处被原样打印）。
    判据取 ``'dsn'`` 字面量（``db_ref['dsn']`` / ``db_ref.get('dsn')``）而不是裸词
    ``dsn``，避免把 ``print("dsn …")`` 这类纯文字误判成泄漏。
    """
    src = TENANTCTL.read_text()
    offenders = [
        lineno
        for lineno, seg in _print_segments(src)
        if ("'dsn'" in seg or '"dsn"' in seg) and "mask_dsn(" not in seg
    ]
    assert not offenders, (
        f"tenantctl.py 第 {offenders} 行的 print 回显了未打码的 DSN——"
        "会连同口令一起留在终端回滚/CI 日志里。套一层 mask_dsn() 即可。"
    )


def test_tenantctl_guard_actually_catches_a_regression() -> None:
    """自检：守卫本身有效——把一处改回裸打印，判据必须能认出来。

    没有这条，上面那条在 AST 改写/重命名后会**静默失效**（扫不到任何东西也"通过"）。
    """
    src = TENANTCTL.read_text()
    mutated = src.replace(
        "print(f\"[tenantctl] 🆕 已创建租户库 {mask_dsn(db_ref['dsn'])}\")",
        "print(f\"[tenantctl] 🆕 已创建租户库 {db_ref['dsn']}\")",
    )
    assert mutated != src, "没找到被替换的那行——tenantctl 的写法变了，本自检需同步"

    offenders = [
        lineno
        for lineno, seg in _print_segments(mutated)
        if ("'dsn'" in seg or '"dsn"' in seg) and "mask_dsn(" not in seg
    ]
    assert offenders, "守卫漏掉了裸打印——判据太松，等于没有"
