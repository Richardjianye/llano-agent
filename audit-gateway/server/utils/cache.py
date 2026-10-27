"""Redis 异步连接池管理器（单例模式）"""

import asyncio
from contextlib import asynccontextmanager

from config import get_settings
from loguru import logger
from redis import asyncio as aioredis
from redis.exceptions import ConnectionError

config = get_settings()


class CacheManager:
    """
    Redis 连接池管理器

    用法:
        # 上下文管理器（推荐）
        async with cache.redis_connection() as redis:
            await redis.set("key", "value", ex=60)
            data = await redis.get("key")

        # 获取连接实例
        redis = cache.get_redis()
    """
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        url = config.redis_sock if config.redis_sock else config.redis_url
        self.pool = aioredis.ConnectionPool.from_url(
            url,
            encoding="utf-8",
            max_connections=20,
            # ⚠️ 这四个值是一起定的，别单独调其中一个。
            #
            # 原来是 `socket_timeout=3 + retry_on_timeout=True + health_check_interval=5`，
            # 实测把「Redis 停顿 3 秒」放大成「请求耗时 9 秒」：3 秒超时 × 3 次尝试。
            # 而 /admin/user/info 正是桌面端可达性探测的那条（客户端 15 秒超时），
            # 9 秒已经吃掉六成预算，实测 /admin/user/refresh 到过 13.4 秒。
            #
            # **对一个此刻正卡住的单线程服务器重试，只是把等待乘以三。**
            # 所以关掉超时重试：快速失败，由调用方决定放行还是拒绝
            # （见 token_manager.is_token_revoked 的分格）。
            #
            # health_check 从 5 秒放到 30 秒：60 秒轮询的场景下，5 秒意味着
            # **每一次请求都要先付一个 PING**，等于把停顿的命中率拉满。
            retry_on_timeout=False,
            health_check_interval=30,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        self._prewarmed = False

    async def prewarm_pool(self):
        """预热连接池，创建初始连接避免首次请求延迟"""
        if self._prewarmed:
            return

        try:
            async with self.redis_connection() as redis:
                await redis.ping()

            async def create_and_ping():
                async with self.redis_connection() as redis:
                    await redis.ping()
                    await asyncio.sleep(0.1)

            tasks = [create_and_ping() for _ in range(3)]
            await asyncio.gather(*tasks, return_exceptions=True)

            self._prewarmed = True
            logger.info("Redis连接池预热完成")
        except Exception as e:
            logger.error(f"Redis连接池预热失败: {e}")

    def get_redis(self):
        """获取Redis连接实例"""
        try:
            return aioredis.Redis(connection_pool=self.pool)
        except ConnectionError as e:
            logger.error(f"Redis连接失败：{e}")
            raise

    @asynccontextmanager
    async def redis_connection(self):
        """上下文管理器方式获取Redis连接"""
        redis = None
        try:
            redis = aioredis.Redis(connection_pool=self.pool)
            yield redis
        except ConnectionError as e:
            logger.error(f"Redis连接失败：{e}")
            raise
        finally:
            if redis:
                await redis.close()

    async def close(self):
        """关闭Redis连接池"""
        if self.pool:
            self.pool = None
            self._prewarmed = False

    async def keep_alive(self):
        """保持连接池活跃，定期ping防止断开"""
        try:
            async def ping_connection():
                async with self.redis_connection() as redis:
                    await redis.ping()

            tasks = [ping_connection() for _ in range(3)]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Redis保持活跃任务出错: {e}")


# ========== 全局便捷函数 ==========

def get_redis():
    """获取Redis连接实例"""
    cache_manager = CacheManager.get_instance()
    return cache_manager.get_redis()


def get_redis_context():
    """获取Redis连接上下文管理器"""
    cache_manager = CacheManager.get_instance()
    return cache_manager.redis_connection()
