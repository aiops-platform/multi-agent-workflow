"""Queue 接口（design §5：双队列 run.trigger + run.command）。

- ``run.trigger``：新 run 触发（含 workflow_snapshot_id）
- ``run.command``：pause / resume / 审批完成后触发命令
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

# 双队列主题
# v5.3 §11 第 5 项：生产形态 topic-per-tenant（broker SASL/ACL 做租户边界），
# 服务发布经 topic_trigger/topic_command 路由；TOPIC_* 为单租户回退/兼容常量。
TOPIC_TRIGGER = "run.trigger"
TOPIC_COMMAND = "run.command"


def topic_trigger(tenant_id: str) -> str:
    return f"run.trigger.{tenant_id}"


def topic_command(tenant_id: str) -> str:
    return f"run.command.{tenant_id}"


class Queue(ABC):
    @abstractmethod
    async def publish(self, topic: str, key: str, message: dict) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str) -> AsyncIterator[dict]:
        """消费 topic 消息流（阻塞迭代，用于 Worker 常驻循环）。"""
        raise NotImplementedError
