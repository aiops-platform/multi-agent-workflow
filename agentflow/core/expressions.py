# -*- coding: utf-8 -*-
"""条件表达式求值（design §8.1 `when` 边 / §8.2 join 语义的基础）。

支持的表达式形态（当前范围，S-010b 实测过的最小集）：
- ``$.nodes.<id>.output == 字面量``
- ``$.nodes.<id>.output.<field> == 字面量``
- ``!=`` / ``==`` 比较

扩展（M2+）：```in / not in / and / or`` 等由 ``expressions.py`` 增量补充。
"""
from __future__ import annotations

from typing import Any


def get_path(data: dict, path: str) -> Any:
    """取 `$.nodes.<id>.output` 或 `$.nodes.<id>.output.<field>` 指向的值。

    路径形如 `nodes.approve-changes.output.approved`（调用方已剥掉 `$.` 前缀）。
    """
    cur: Any = data
    for part in path.strip().split("."):
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def coerce_literal(tok: str) -> Any:
    tok = tok.strip()
    if tok in ("null", "None"):
        return None
    if tok in ("true", "True"):
        return True
    if tok in ("false", "False"):
        return False
    if (tok.startswith("'") and tok.endswith("'")) or (
        tok.startswith('"') and tok.endswith('"')
    ):
        return tok[1:-1]
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return tok


def eval_condition(expr: str, node_states: dict[str, dict]) -> bool:
    """求值 `$.nodes.X.output[.field] op 字面量`，op ∈ {==, !=}。

    引用不存在的输出（如 skipped 节点的 output=None，或前置未执行）一律判为不满足。
    """
    expr = expr.strip()
    for op in ("!=", "=="):
        if op in expr:
            left, _, right = expr.partition(op)
            actual = get_path(node_states, left.replace("$.nodes.", "", 1))
            expected = coerce_literal(right)
            return actual != expected if op == "!=" else actual == expected
    raise ValueError(f"不支持的条件表达式: {expr!r}（当前仅支持 == / !=）")
