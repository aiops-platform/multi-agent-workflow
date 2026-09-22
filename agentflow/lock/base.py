"""Lock 接口（design §5：分布式锁，审批 CAS / Worker 抢占用）。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Lock(ABC):
    @abstractmethod
    async def acquire(self, key: str, ttl: float = 30.0) -> bool:
        """非阻塞获取，成功返回 True。"""

    @abstractmethod
    async def release(self, key: str) -> None: ...

    @abstractmethod
    async def refresh(self, key: str, ttl: float = 30.0) -> bool:
        """给自己**仍持有**的锁续期；key 不存在或已被别人接管 → ``False``。

        存在的理由是「租约」：执行体（Worker 跑一条 run）可能跑几分钟到几十分钟，
        远超一个合理的 TTL —— 没有续期就只能把 TTL 设得很大，那等于没有租约
        （进程死了要等很久才被发现）。**TTL 短 + 定期续** 才是"进程还活着"的
        惯用判据：进程一死，续期停止，TTL 到期后锁自然消失。

        ⚠️ **必须校验持有者**（和 ``release`` 同理）：不校验的话，在"别人已经接管"
        的竞态里会替**别人**续期，把一条本该被判定为僵尸的租约续成"活着"。
        """


    @abstractmethod
    async def is_locked(self, key: str) -> bool: ...

    async def __aenter__(self) -> Lock:
        # 子类可覆写；这里仅作为文档性的默认（blocking acquire 由调用方处理）
        return self

    async def __aexit__(self, *exc) -> None:
        pass
