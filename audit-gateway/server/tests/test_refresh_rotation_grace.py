"""刷新令牌轮换的宽限重放（2026-09-29）

## 现象

生产 `api_keys`：09-17 ~ 09-28 有 21 把设备凭据在 21:00—08:00 无人操作时被客户端
自动作废（14 人），第二天早上只能重新登录。只有「鉴权失败 → 清会话」这条路会在
夜里吊销设备凭据。

## 这组用例钉住的那一半

refresh_token 一次性。刷新请求到了网关、旧令牌已作废、新的一对已签发，但回包在路上
丢了（休眠唤醒时网络半通、客户端 15 秒超时）—— 客户端手里只剩那枚作废的旧令牌。
判据是「继任者有没有被用过」：没用过 = 客户端没收到，允许再换一次；用过 = 真重放，拒绝。

（客户端那一半 —— 刷新成功后后续校验超时把新令牌弄丢 —— 在
`apps/desktop/electron/product-fault-matrix.test.ts`。）

走真实 JWT 签发与真实 TokenStateManager，只把 Redis 换成内存字典。
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest

from model.request.user import RefreshToken
from model.response import UserInfo
from router.admin import user as user_router
from utils.token_manager import TokenStateManager


class _MemoryRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def delete(self, key):
        self.kv.pop(key, None)
        self.sets.pop(key, None)

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def expire(self, key, ttl):
        return True

    async def smembers(self, key):
        return set(self.sets.get(key, set()))


@pytest.fixture
def gateway(monkeypatch):
    redis = _MemoryRedis()
    manager = TokenStateManager()

    @contextlib.asynccontextmanager
    async def _conn():
        yield redis

    manager.cache = SimpleNamespace(redis_connection=_conn)
    monkeypatch.setattr(user_router, "token_manager", manager)

    account = SimpleNamespace(
        id=1, uid="u1", nickname="admin", permission="employee", status=1
    )

    async def _get_user_info(uid=None, id=None):
        return account if uid == "u1" else None

    async def _to_user_info(_user, *, password_change_required=False):
        return UserInfo(id=1, uid="u1", username="admin", permission="employee")

    monkeypatch.setattr(user_router, "get_user_info", _get_user_info)
    monkeypatch.setattr(user_router, "to_user_info", _to_user_info)
    return SimpleNamespace(redis=redis, manager=manager, account=account)


async def _login(gateway):
    access = user_router.jwt_auth.create_access_token(gateway.account)
    refresh = user_router.jwt_auth.create_refresh_token(gateway.account)
    await gateway.manager.store_token(access, "1")
    await gateway.manager.store_token(refresh, "1")
    return refresh


async def _refresh(token):
    request = SimpleNamespace(cookies={})
    resp = await user_router.refresh_token(RefreshToken(refresh_token=token), request)
    if hasattr(resp, "body"):
        body = json.loads(resp.body)
        return body["code"], body.get("data")
    return resp.code, resp.data


async def test_正常轮换后旧令牌不能再用_前提是继任者已被用过(gateway):
    first = await _login(gateway)
    code, data = await _refresh(first)
    assert code == 0
    second = data["refresh_token"]

    # 客户端收到并用了继任者
    code, _ = await _refresh(second)
    assert code == 0

    # 这时再拿第一枚来换，就是真正的重放
    code, _ = await _refresh(first)
    assert code == 10003


async def test_回包丢了_旧令牌可以再换一次(gateway):
    first = await _login(gateway)
    code, lost = await _refresh(first)  # 回包丢了，客户端没拿到 lost
    assert code == 0

    code, data = await _refresh(first)
    assert code == 0, "继任者从没被用过，说明客户端没收到那次回包"

    # 没人收到的那对被作废了，不留活令牌
    assert await gateway.manager.is_token_revoked(lost["refresh_token"]) is True
    assert await gateway.manager.is_token_revoked(lost["access_token"]) is True

    # 新拿到的这对正常可用
    code, _ = await _refresh(data["refresh_token"])
    assert code == 0


async def test_连续丢两次也救得回来(gateway):
    first = await _login(gateway)
    await _refresh(first)
    await _refresh(first)

    code, _ = await _refresh(first)
    assert code == 0


async def test_退出登录吊销的令牌不走宽限(gateway):
    token = await _login(gateway)
    await gateway.manager.revoke_token(token, reason="logout")

    code, _ = await _refresh(token)
    assert code == 10003


async def test_管理员批量吊销后不走宽限(gateway):
    first = await _login(gateway)
    code, data = await _refresh(first)
    assert code == 0

    # 改密码 / 停用：用户名下所有活令牌都作废，包括那个没被用过的继任者
    await gateway.manager.revoke_user_tokens("u1", reason="password_reset")

    code, _ = await _refresh(first)
    assert code == 10003
    code, _ = await _refresh(data["refresh_token"])
    assert code == 10003
