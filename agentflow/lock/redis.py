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

    def _is_mine(self, got) -> bool:
        """redis get 默认返回 bytes，与 str token 比较需兼容。"""
        return got is not None and (
            got == self._token or (isinstance(got, bytes) and got.decode() == self._token)
        )

    async def release(self, key: str) -> None:
        # 仅释放自己持有的锁（token 校验，防止误删他人锁）
        if self._is_mine(await self._redis.get(key)):
            await self._redis.delete(key)

    async def refresh(self, key: str, ttl: float = 30.0) -> bool:
        """续期；只在**仍由自己持有**时成功。

        「校验 token + 续期」必须是一个原子操作。拆成 ``GET`` 再 ``PEXPIRE``
        的话，两步之间可能"我的锁刚过期、别人抢到了、我再去 PEXPIRE" ——
        **替接管者续了期**。

        ## 为什么用 `WATCH/MULTI` 而不是 Lua

        Redis 社区的标准做法是 Lua（`GET` 比对后 `PEXPIRE`），但 **`fakeredis`
        不带 `eval`**（要额外的 lupa 依赖），于是那段逻辑在单测里根本跑不到 ——
        而"续期只续自己的"恰恰是最该被测住的一条。`WATCH/MULTI` 乐观事务
        在真 redis 与 fakeredis 上都支持，且同样是原子的：`WATCH` 之后的
        `PEXPIRE` 若发现 key 被改过（含被删/被抢），整个事务不执行。

        代价是并发冲突时要重试；这里冲突即"锁已易主"，**不重试是对的** ——
        正确反应就是返回 ``False``。
        """
        from redis.exceptions import WatchError

        async with self._redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if not self._is_mine(await pipe.get(key)):
                    await pipe.unwatch()
                    return False
                pipe.multi()
                pipe.pexpire(key, int(ttl * 1000))
                res = await pipe.execute()
            except WatchError:
                return False
        return bool(res and res[0])

    async def is_locked(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def close(self) -> None:
        await self._redis.aclose()
