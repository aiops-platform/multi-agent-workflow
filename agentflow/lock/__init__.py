"""Lock 适配层：配置驱动切换（design §5）。"""
from __future__ import annotations

from ..config import Settings
from .base import Lock
from .memory import InMemoryLock

#: 「这条 run 有执行者吗」—— **执行租约**。
#:
#: ## 为什么需要它
#:
#: Worker 认领一条 run 之后，绑定关系（`_tasks` / `_executors`）只存在于**进程内存**
#: 里；数据库里那条 `running` 只有一个词，没有任何字段指出**谁**在跑它。于是 Worker
#: 暴毙后：别的 Worker 看到 `running` 不会碰（接单 CAS 只从 `queued` 接），而它既不能
#: 被 trigger 也不能被 resume（CAS 只从 `paused`/`waiting_approval` 接）—— **僵尸**。
#:
#: 租约把那个绑定搬到**共享存储**里，让"还有没有执行者"变成可判定的：TTL 60s + 每
#: 20s 续期，进程一死续期就停、TTL 到期键消失。
#:
#: ⚠️ **它只回答"有没有"，不回答"是谁"** —— 锁的值是持有者的 token，但消费方
#: （暂停分流、UI 展示）只关心在不在。
LEASE_TTL_SEC = 60.0
#: 续期间隔取 TTL 的 1/3：容忍两次连续失败（网络抖动）而不误判成"执行者已死"。
LEASE_REFRESH_SEC = 20.0


def run_exec_lease_key(run_id: str) -> str:
    """执行租约的键名。**只有这一处实现** —— worker 持它、API 查它，两处必须同名。"""
    return f"run-exec:{run_id}"


def build_lock(settings: Settings) -> Lock:
    if settings.lock == "memory":
        return InMemoryLock()
    if settings.lock == "redis":
        from .redis import RedisLock

        return RedisLock(settings.redis_url)
    raise ValueError(f"未知 lock 后端: {settings.lock!r}")
