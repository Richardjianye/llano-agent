"""撤销检查回归：Redis 连接故障与明确拒绝应采用不同处理策略。"""

from __future__ import annotations

import contextlib

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from utils.token_manager import TokenStateManager


def _manager(raises=None, revoked=False, active=True):
    """造一个 TokenStateManager，其 redis_connection 按需抛错或返回预设值。"""
    mgr = TokenStateManager()

    class _FakeRedis:
        async def get(self, key):
            if key.startswith("revoked_token:"):
                return '{"reason":"manual"}' if revoked else None
            return '{"uid":"u1"}' if active else None

    @contextlib.asynccontextmanager
    async def _conn():
        if raises is not None:
            raise raises
        yield _FakeRedis()

    mgr.cache = type("_C", (), {"redis_connection": staticmethod(_conn)})()
    # 解码那一步走真实逻辑会需要真 JWT；这里只测 Redis 那几格
    mgr._decode_token = lambda token: {"jti": "j1", "sub": "u1", "exp": 9999999999}
    return mgr


@pytest.mark.parametrize("boom", [
    RedisTimeoutError("Timeout reading from 127.0.0.1:6379"),
    RedisConnectionError("Connection refused"),
    OSError("WinError 10054"),
    TimeoutError(),
])
async def test_redis_silence_lets_the_user_through(boom):
    """**Redis 没答上来 = 放行。**

    这一条直接对应那次事故：把它改回 True，休眠唤醒的用户就会重新被
    「你已被管理员下线」顶出去，而且测试全绿。
    """
    assert await _manager(raises=boom).is_token_revoked("t") is False


async def test_an_answer_we_do_not_understand_is_treated_as_suspicious():
    """Redis 应答了但报了个我们不认识的错 —— 服务是活的，按可疑处理。

    与上一条的区别是「答了」和「没答」，不是「异常类型好看不好看」。
    """
    assert await _manager(raises=ResponseError("WRONGTYPE")).is_token_revoked("t") is True


async def test_a_real_revocation_still_blocks():
    """反向基线。放行那一格不能宽到把真正的撤销也放过去 ——
    否则这组用例就变成了「撤销功能已删除」的证明。"""
    assert await _manager(revoked=True).is_token_revoked("t") is True


async def test_a_token_missing_from_the_whitelist_is_rejected():
    """白名单里没有 = 拒绝。这是 Redis **答了**的情况，与超时不同格。

    ⚠️ 已知副作用：网关机器重启后 Redis 若丢了 active_token:*，所有人都会
    落到这一格 —— 那是另一个问题（白名单存在易失存储里），已记 TODO，
    不在这条用例的范围内。
    """
    assert await _manager(active=False).is_token_revoked("t") is True


def test_the_two_failure_modes_are_not_merged_back():
    """扫源码：放行那一格必须还在。

    合并回一个 `except Exception` 不会让上面任何一条变红吗？会 —— 但只有在
    有人跑测试的时候。这条的作用是让**读代码的人**先看见这里有两格。
    """
    import inspect

    source = inspect.getsource(TokenStateManager.is_token_revoked)

    assert "RedisTimeoutError" in source, "超时必须单独成格：它是「没答」，不是「已撤销」"
    assert source.count("except") >= 3, "至少三格：没答 / 答了但不认识 / 其他"
