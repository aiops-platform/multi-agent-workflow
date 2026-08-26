# -*- coding: utf-8 -*-
"""Redis Lock（design §5 分布式锁，M6 生产适配器）。

基于 SET key value NX PX ttl：CAS 式原子获取；释放仅删自己持有的 key。
"""
from __future__ import annotations

import uuid


class RedisLock:
    def __init__(self, url: str, *, client=None) -> None:
        if client is not None:  # 测试注入（fakeredis）
            self._redis = client
        else:
            from redis.asyncio import from_url

            self._redis = from_url(url)
        self._token = uuid.uuid4().hex

    async def acquire(self, key: str, ttl: float = 30.0) -> bool:
        """非阻塞获取：SET NX PX（原子，仅当 key 不存在才成功）。"""
        ok = await self._redis.set(key, self._token, nx=True, px=int(ttl * 1000))
        return bool(ok)

    async def release(self, key: str) -> None:
        # 仅释放自己持有的锁（token 校验，防止误删他人锁）
        # redis get 默认返回 bytes，与 str token 比较需兼容
        got = await self._redis.get(key)
        if got is not None and (
            got == self._token or (isinstance(got, bytes) and got.decode() == self._token)
        ):
            await self._redis.delete(key)

    async def is_locked(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def close(self) -> None:
        await self._redis.aclose()
