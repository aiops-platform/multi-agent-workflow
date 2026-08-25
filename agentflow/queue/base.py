# -*- coding: utf-8 -*-
"""Queue 接口（design §5：双队列 run.trigger + run.command）。

- ``run.trigger``：新 run 触发（含 workflow_snapshot_id）
- ``run.command``：pause / resume / 审批完成后触发命令
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

# 双队列主题
TOPIC_TRIGGER = "run.trigger"
TOPIC_COMMAND = "run.command"


class Queue(ABC):
    @abstractmethod
    async def publish(self, topic: str, key: str, message: dict) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str) -> AsyncIterator[dict]:
        """消费 topic 消息流（阻塞迭代，用于 Worker 常驻循环）。"""
        raise NotImplementedError
