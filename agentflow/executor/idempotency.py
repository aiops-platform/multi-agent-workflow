# -*- coding: utf-8 -*-
"""副作用幂等（design §8.4：execution_id + external_operation_id）。

- 每个节点执行生成唯一 ``execution_id``，写 node_attempts 表。
- 副作用节点（committer / infra-remediator）带 ``external_operation_id``
  （PR number / deployment ID 等）；重跑时先查同 run 同节点同外部操作的成功记录，
  命中则**复用结果**（§8.4.2），避免重复副作用。
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from ..statestore.base import StateStore

Action = Callable[[], Awaitable[Any]]


def new_execution_id(run_id: str, node_id: str, attempt: int) -> str:
    return f"{run_id}_{node_id}_{attempt}_{uuid.uuid4().hex[:12]}"


async def execute_with_idempotency(
    store: StateStore,
    run_id: str,
    node_id: str,
    attempt: int,
    action: Action,
    *,
    external_operation_id: str | None = None,
    max_attempts: int = 1,
    on_error: Callable[[Exception], Awaitable[Any]] | None = None,
) -> Any:
    """幂等执行：先复用成功记录，否则执行并记录尝试（含 retry）。

    ``external_operation_id`` 存在时，先查同 run 同节点同外部操作的成功记录，
    命中直接返回（§8.4.2 第 1 步）。失败按 ``max_attempts`` 重试；
    耗尽后若提供 ``on_error`` 则调用（如负证据节点 on_failure: continue），
    否则抛出最后一次异常。
    """
    if external_operation_id:
        existing = await store.get_succeeded_attempt(
            run_id, node_id, external_operation_id
        )
        if existing and existing["status"] == "succeeded":
            return existing["output"]

    last_exc: Exception | None = None
    for i in range(max_attempts):
        execution_id = new_execution_id(run_id, node_id, attempt + i)
        try:
            result = await action()
            await store.record_attempt(
                run_id,
                node_id,
                attempt + i,
                execution_id,
                "succeeded",
                output=result,
                external_operation_id=external_operation_id,
            )
            return result
        except Exception as exc:  # noqa: BLE001 - 需要记录失败并重试
            last_exc = exc
            await store.record_attempt(
                run_id,
                node_id,
                attempt + i,
                execution_id,
                "failed",
                error=str(exc),
                external_operation_id=external_operation_id,
            )
    assert last_exc is not None
    if on_error is not None:
        return await on_error(last_exc)
    raise last_exc
