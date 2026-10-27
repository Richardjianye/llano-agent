"""对外成员搜索（OSS 等第三方平台）—— /open/v1（2026-10-19）

第三方会拿网关 uid + 昵称建自己的权限表，所以这组守的是：
- 只有登记了 directory.read 的机密应用能换到令牌；员工/管理员令牌不行
- 回的字段就是 uid / nickname / status，uid 改名不变
- 不能不带条件翻全员；有频率上限；每次调用留痕，关键字不落原文
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import Base, OpenApiCallLog, User
from services import open_directory as od

SECRET = "s3cret-for-oss"


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def expire(self, key, seconds):
        return True


class _Ctx:
    def __init__(self, redis):
        self.redis = redis

    async def __aenter__(self):
        return self.redis

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'open.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[User.__table__, OpenApiCallLog.__table__]))
        await conn.run_sync(lambda c: od._registry.create_all(c))
        await conn.execute(od.sso_clients_view.insert(), [
            {"client_id": "oss", "environment": "dev", "client_type": "confidential",
             "allowed_scopes": "openid directory.read", "secret_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
             "enabled": True},
            {"client_id": "login-only", "environment": "dev", "client_type": "confidential",
             "allowed_scopes": "openid profile", "secret_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
             "enabled": True},
            {"client_id": "off", "environment": "dev", "client_type": "confidential",
             "allowed_scopes": "directory.read", "secret_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
             "enabled": False},
            {"client_id": "prod-app", "environment": "prod", "client_type": "confidential",
             "allowed_scopes": "directory.read", "secret_hash": hashlib.sha256(SECRET.encode()).hexdigest(),
             "enabled": True},
        ])
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def factory():
        return maker

    import db as db_pkg
    monkeypatch.setattr(db_pkg, "init_async_session", factory)
    redis = FakeRedis()
    import router.open_api as open_router
    monkeypatch.setattr(open_router, "get_redis_context", lambda: _Ctx(redis))
    monkeypatch.setattr(od, "current_environment", lambda: "dev")

    async with maker() as session:
        session.add(User(uid="u-yh", username="yuanhe", realname="源贺", nickname="源贺",
                         password="x", permission="manager", status=1))
        session.add(User(uid="u-left", username="left", realname="离职者", nickname="离职者",
                         password="x", permission="employee", status=0))
        for i in range(60):
            session.add(User(uid=f"u{i:02d}", username=f"emp{i:02d}", realname=f"员工{i:02d}",
                             nickname=f"员工{i:02d}", password="x", permission="employee", status=1))
        await session.commit()

    app = FastAPI()
    app.include_router(open_router.router)
    try:
        yield TestClient(app), maker, redis
    finally:
        await engine.dispose()


def _token(client, client_id="oss", secret=SECRET, basic=True):
    if basic:
        auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        return client.post("/open/v1/token", data={"grant_type": "client_credentials"},
                           headers={"Authorization": f"Basic {auth}"})
    return client.post("/open/v1/token", data={"grant_type": "client_credentials",
                                               "client_id": client_id, "client_secret": secret})


def _search(client, token, **params):
    return client.get("/open/v1/members/search", params=params,
                      headers={"Authorization": f"Bearer {token}"})


def test_换令牌后按昵称搜到uid与昵称(env):
    client, _, _ = env
    resp = _token(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 600
    assert resp.headers["cache-control"] == "no-store"

    data = _search(client, body["access_token"], keyword="源贺").json()
    assert data["items"] == [{"uid": "u-yh", "nickname": "源贺", "status": "active"}]
    assert data["total"] == 1 and data["has_more"] is False


def test_client_secret_post也可以(env):
    client, _, _ = env
    assert _token(client, basic=False).status_code == 200


@pytest.mark.parametrize("client_id,secret,status,error", [
    ("oss", "wrong", 401, "invalid_client"),
    ("nobody", SECRET, 401, "invalid_client"),
    ("off", SECRET, 401, "invalid_client"),
    ("prod-app", SECRET, 401, "invalid_client"),       # 环境不对
    ("login-only", SECRET, 403, "unauthorized_client"),  # 没登记 directory.read
])
def test_应用凭据或授权不对拿不到令牌(env, client_id, secret, status, error):
    client, _, _ = env
    resp = _token(client, client_id, secret)
    assert resp.status_code == status
    assert resp.json()["error"] == error


def test_只支持client_credentials与directory_read(env):
    client, _, _ = env
    auth = base64.b64encode(f"oss:{SECRET}".encode()).decode()
    r = client.post("/open/v1/token", data={"grant_type": "password"}, headers={"Authorization": f"Basic {auth}"})
    assert r.json()["error"] == "unsupported_grant_type"
    r = client.post("/open/v1/token", data={"grant_type": "client_credentials", "scope": "directory.read openid"},
                    headers={"Authorization": f"Basic {auth}"})
    assert r.json()["error"] == "invalid_scope"


def test_没有或伪造令牌不能搜(env):
    client, _, _ = env
    assert client.get("/open/v1/members/search", params={"keyword": "源"}).status_code == 401
    assert _search(client, "forged", keyword="源").status_code == 401


def test_不带条件不能翻全员(env):
    client, _, _ = env
    token = _token(client).json()["access_token"]
    resp = _search(client, token)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_request"
    assert _search(client, token, limit=51).status_code == 422


def test_按uid批量同步并能看到离职(env):
    client, _, _ = env
    token = _token(client).json()["access_token"]
    active_only = _search(client, token, uids="u-yh,u-left").json()["items"]
    assert [i["uid"] for i in active_only] == ["u-yh"]
    everyone = _search(client, token, uids="u-yh,u-left", status="all").json()["items"]
    assert {i["uid"]: i["status"] for i in everyone} == {"u-yh": "active", "u-left": "disabled"}


async def test_改名改昵称后uid不变(env):
    client, maker, _ = env
    token = _token(client).json()["access_token"]
    async with maker() as session:
        await session.execute(update(User).where(User.uid == "u-yh").values(
            nickname="源贺（设计）", username="yuanhe2", realname="源贺二"))
        await session.commit()
    item = _search(client, token, uids="u-yh").json()["items"][0]
    assert item == {"uid": "u-yh", "nickname": "源贺（设计）", "status": "active"}


def test_分页上限50(env):
    client, _, _ = env
    token = _token(client).json()["access_token"]
    first = _search(client, token, keyword="员工", limit=50).json()
    assert len(first["items"]) == 50 and first["total"] == 60 and first["has_more"] is True


def test_超过频率上限返回429(env, monkeypatch):
    client, _, _ = env
    from config import get_settings
    monkeypatch.setattr(get_settings(), "open_api_rate_limit_per_minute", 3)
    token = _token(client).json()["access_token"]  # 第 1 次
    assert _search(client, token, keyword="员工").status_code == 200  # 2
    assert _search(client, token, keyword="员工").status_code == 200  # 3
    resp = _search(client, token, keyword="员工")
    assert resp.status_code == 429 and resp.json()["error"] == "rate_limited"


async def test_每次调用留痕且不存关键字原文(env):
    client, maker, _ = env
    token = _token(client).json()["access_token"]
    _search(client, token, keyword="源贺")
    _token(client, "oss", "wrong")
    async with maker() as session:
        rows = list((await session.execute(select(OpenApiCallLog).order_by(OpenApiCallLog.id))).scalars())
    assert [(r.client_id, r.endpoint, r.status) for r in rows] == [
        ("oss", "token", "ok"), ("oss", "members.search", "ok"), ("oss", "token", "invalid_client")]
    assert rows[1].result_count == 1
    assert "源贺" not in (rows[1].query_summary or "")
    assert rows[1].query_digest


async def test_审计写不进去就不给数据(env, monkeypatch):
    client, _, _ = env
    token = _token(client).json()["access_token"]

    real = od.record_call

    async def failing_record(**kw):
        if kw.get("strict"):
            raise od.OpenApiError("temporarily_unavailable", "暂时无法处理请求，请稍后重试", 503)
        return await real(**kw)

    import router.open_api as open_router
    monkeypatch.setattr(open_router.od, "record_call", failing_record)
    resp = _search(client, token, keyword="源贺")
    assert resp.status_code == 503
    assert "items" not in resp.json()


def test_sso_clients表还没建时一律invalid_client(tmp_path, monkeypatch):
    import asyncio

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    maker = async_sessionmaker(engine)

    async def factory():
        return maker

    import db as db_pkg
    monkeypatch.setattr(db_pkg, "init_async_session", factory)
    try:
        assert asyncio.run(od.load_client("oss")) is None
    finally:
        asyncio.run(engine.dispose())
