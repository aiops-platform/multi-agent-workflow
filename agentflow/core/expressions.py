"""条件表达式求值（design §8.1 `when` 边 / §8.2 join 语义的基础）。

支持的表达式形态（当前范围，S-010b 实测过的最小集）：
- ``$.nodes.<id>.output == 字面量``
- ``$.nodes.<id>.output.<field> == 字面量``
- ``$.inputs.<field> == 字面量``（2026-09-22 加：让图能按**工单来源**这类入参分流）
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


#: 条件表达式左侧**允许的根前缀**。写别的（打错、或从别的引擎抄来的）一律报错。
NODE_REF_PREFIX = "$.nodes."
INPUTS_REF_PREFIX = "$.inputs."


def _resolve_operand(ref: str, node_states: dict, inputs: dict) -> Any:
    """求值左侧引用：`$.nodes.X.output[.field]` 或 `$.inputs[.field]`。

    取不到值一律返回 ``None``（调用方按"条件不满足"处理）—— 这是**既有且承重**的语义：
    skipped 节点的 `output` 就是 `None`，`when` 必须能对它求值成"不满足"。

    ⚠️ 但**根前缀不认识时要报错**，不能也返回 None：那样 `$.inputs.x == 'y'` 会静默恒假、
    `!= 'y'` 会静默恒真，而**页面上看不出区别** —— 判错的方向可能是"该走的分支没走"。
    """
    ref = ref.strip()
    if ref.startswith(NODE_REF_PREFIX):
        return get_path(node_states, ref[len(NODE_REF_PREFIX):])
    if ref.startswith(INPUTS_REF_PREFIX):
        return get_path(inputs, ref[len(INPUTS_REF_PREFIX):])
    raise ValueError(
        f"条件表达式的引用必须以 {NODE_REF_PREFIX!r} 或 {INPUTS_REF_PREFIX!r} 开头：{ref!r}"
    )


def eval_condition(expr: str, node_states: dict[str, dict], inputs: dict | None = None) -> bool:
    """求值 `<引用> op 字面量`，op ∈ {==, !=}；引用见 :func:`_resolve_operand`。

    引用**取不到值**（skipped 节点的 output=None、前置未执行、字段不存在）→ 判为不满足。

    ## `$.inputs.*` 是 2026-09-22 加的；以前那个写法是**静默错**

    此前只有 `$.nodes.*`，实现是 `left.replace("$.nodes.", "", 1)` 再去 node_states 里走。
    把一个 `$.inputs.origin` 放进去，`replace` 不生效 → 原样当路径走 → 走不到 → `None`
    → **`== 字面量` 恒假、`!= 字面量` 恒真，一声不吭**。
    也就是说"按工单来源分流"这类条件**写下去看着像生效了，实际永远只走同一条分支**。

    现在按前缀分派，且**不认识的前缀直接报错**——宁可加载期就炸，也不要一条永远为真的 `when`。
    （加载期那道见 `core/dag.py` 的 `_check_when_refs`，只在 `strict=True` 时跑。）
    """
    expr = expr.strip()
    for op in ("!=", "=="):
        if op in expr:
            left, _, right = expr.partition(op)
            actual = _resolve_operand(left, node_states, inputs or {})
            expected = coerce_literal(right)
            return actual != expected if op == "!=" else actual == expected
    raise ValueError(f"不支持的条件表达式: {expr!r}（当前仅支持 == / !=）")
