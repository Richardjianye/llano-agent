"""钉钉登录的浏览器状态、企业身份与账号绑定边界。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from model.database import ExternalIdentity
from model.response import UserInfo
from router.admin import dingtalk
from services import dingtalk_identity, external_identity
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex, nx):
        assert ex == 300 and nx is True
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key):
        return self.values.pop(key, None)


class FakeCache:
    def __init__(self):
        self.redis = FakeRedis()

    @asynccontextmanager
    async def redis_connection(self):
        yield self.redis


@pytest.fixture
def browser(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(dingtalk.CacheManager, "get_instance", lambda: cache)
    monkeypatch.setattr(dingtalk, "configured", lambda settings=None: True)
    monkeypatch.setattr(dingtalk, "get_settings", lambda: SimpleNamespace(is_prod=False))
    monkeypatch.setattr(dingtalk, "authorization_url", lambda state: "https://login.dingtalk.com/test?state=" + state)
    app = FastAPI()
    app.include_router(dingtalk.router, prefix="/admin")
    with TestClient(app, follow_redirects=False) as client:
        yield app, client, cache


def test_oauth_callback_requires_same_browser_and_consumes_state(browser, monkeypatch):
    _app, client, cache = browser
    resolve = AsyncMock()
    monkeypatch.setattr(dingtalk, "resolve_identity", resolve)

    started = client.get("/admin/user/dingtalk/login")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    assert started.status_code in (302, 307)
    assert "HttpOnly" in started.headers["set-cookie"]

    rejected = client.get("/admin/user/dingtalk/callback?authCode=code&state=wrong")
    assert "dingtalk_error=state" in rejected.headers["location"]
    assert cache.redis.values["dingtalk:oauth:" + state] == "login"
    resolve.assert_not_awaited()

    monkeypatch.setattr(dingtalk, "find_identity", AsyncMock(return_value=None))
    resolve.return_value = dingtalk_identity.DingTalkIdentity("corp", "union", "member")
    unbound = client.get(f"/admin/user/dingtalk/callback?authCode=code&state={state}")
    assert "dingtalk_error=unbound" in unbound.headers["location"]
    assert cache.redis.values == {}
    replay = client.get(f"/admin/user/dingtalk/callback?authCode=code&state={state}")
    assert "dingtalk_error=state" in replay.headers["location"]
    resolve.assert_awaited_once()


def test_bind_requires_same_logged_in_user_after_dingtalk_callback(browser, monkeypatch):
    app, client, _cache = browser
    user = UserInfo(id=7, uid="local-uid", permission="employee", status=1)
    app.dependency_overrides[dingtalk.require_login] = lambda: user
    monkeypatch.setattr(dingtalk, "resolve_identity", AsyncMock(return_value=
                        dingtalk_identity.DingTalkIdentity("corp", "union", "member")))
    monkeypatch.setattr(dingtalk, "get_user_info", AsyncMock(return_value=SimpleNamespace(status=1)))
    bound = AsyncMock()
    monkeypatch.setattr(dingtalk, "bind_identity", bound)

    started = client.get("/admin/user/dingtalk/bind")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    monkeypatch.setattr(dingtalk, "get_current_user", AsyncMock(return_value=
                        UserInfo(id=8, uid="different-uid", permission="employee")))
    rejected = client.get(f"/admin/user/dingtalk/callback?authCode=code&state={state}")
    assert "dingtalk_error=session" in rejected.headers["location"]
    bound.assert_not_awaited()

    started = client.get("/admin/user/dingtalk/bind")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    monkeypatch.setattr(dingtalk, "get_current_user", AsyncMock(return_value=user))
    accepted = client.get(f"/admin/user/dingtalk/callback?authCode=code&state={state}")
    assert accepted.headers["location"] == "/?dingtalk=bound"
    bound.assert_awaited_once_with("dingtalk", "corp", "union", "local-uid", "member")


def test_bound_member_gets_existing_gateway_session_cookies(browser, monkeypatch):
    _app, client, _cache = browser
    monkeypatch.setattr(dingtalk, "resolve_identity", AsyncMock(return_value=
                        dingtalk_identity.DingTalkIdentity("corp", "union", "member")))
    monkeypatch.setattr(dingtalk, "find_identity", AsyncMock(return_value=
                        SimpleNamespace(user_uid="existing-uid")))
    monkeypatch.setattr(dingtalk, "get_user_info", AsyncMock(return_value=
                        SimpleNamespace(id=3, uid="existing-uid", status=1)))
    minted = AsyncMock(return_value=("new-access", "new-refresh"))
    monkeypatch.setattr(dingtalk, "mint_user_tokens", minted)

    started = client.get("/admin/user/dingtalk/login")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    completed = client.get(f"/admin/user/dingtalk/callback?authCode=code&state={state}")

    assert completed.headers["location"] == "/"
    cookies = completed.headers.get_list("set-cookie")
    assert any("access_token=new-access" in cookie for cookie in cookies)
    assert any("refresh_token=new-refresh" in cookie for cookie in cookies)
    minted.assert_awaited_once()


def test_bound_member_returns_to_pending_sso_authorization(browser, monkeypatch):
    _app, client, cache = browser
    monkeypatch.setattr(dingtalk, "resolve_identity", AsyncMock(return_value=
                        dingtalk_identity.DingTalkIdentity("corp", "union", "member")))
    monkeypatch.setattr(dingtalk, "find_identity", AsyncMock(return_value=
                        SimpleNamespace(user_uid="existing-uid")))
    monkeypatch.setattr(dingtalk, "get_user_info", AsyncMock(return_value=
                        SimpleNamespace(id=3, uid="existing-uid", status=1, account_kind="internal")))
    monkeypatch.setattr(dingtalk, "mint_user_tokens", AsyncMock(return_value=("new-access", "new-refresh")))
    continuation = "/sso/authorize?client_id=artifact&state=test"
    started = client.get("/admin/user/dingtalk/login", params={"continue": continuation})
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    assert cache.redis.values["dingtalk:oauth:" + state] == "login:" + continuation
    completed = client.get(f"/admin/user/dingtalk/callback?authCode=code&state={state}")
    assert completed.headers["location"] == continuation
    assert any("access_token=new-access" in cookie for cookie in completed.headers.get_list("set-cookie"))


def test_authorization_requests_profile_read_permission():
    cfg = SimpleNamespace(
        dingtalk_client_id="app", dingtalk_client_secret="secret",
        dingtalk_corp_id="corp",
        dingtalk_redirect_uri="https://example.com/admin/user/dingtalk/callback",
    )
    query = parse_qs(urlparse(dingtalk_identity.authorization_url("state", settings=cfg)).query)
    assert query["scope"] == ["openid corpid Contact.User.Read"]


@pytest.mark.asyncio
async def test_provider_rejects_wrong_corporation_before_identity_lookup(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"accessToken": "user-token", "corpId": "wrong-corp"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(dingtalk_identity.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=transport, **kwargs))
    cfg = SimpleNamespace(
        dingtalk_client_id="app", dingtalk_client_secret="secret",
        dingtalk_corp_id="expected-corp",
        dingtalk_redirect_uri="https://example.com/admin/user/dingtalk/callback",
    )

    with pytest.raises(dingtalk_identity.DingTalkAuthError):
        await dingtalk_identity.resolve_identity("code", settings=cfg)
    assert seen == ["/v1.0/oauth2/userAccessToken"]


@pytest.mark.asyncio
async def test_provider_resolves_only_internal_member(monkeypatch):
    seen = []
    dingtalk_identity._app_token_cache.clear()

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        if request.url.path.endswith("/userAccessToken"):
            return httpx.Response(200, json={"accessToken": "user-token", "corpId": "corp"})
        if request.url.path.endswith("/contact/users/me"):
            assert request.headers["x-acs-dingtalk-access-token"] == "user-token"
            return httpx.Response(200, json={"unionId": "union"})
        if request.url.path.endswith("/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})
        assert request.url.params["access_token"] == "app-token"
        return httpx.Response(200, json={"errcode": 0,
                                         "result": {"contact_type": 0, "userid": "member"}})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(dingtalk_identity.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=transport, **kwargs))
    cfg = SimpleNamespace(
        dingtalk_client_id="app", dingtalk_client_secret="secret",
        dingtalk_corp_id="corp",
        dingtalk_redirect_uri="https://example.com/admin/user/dingtalk/callback",
    )

    identity = await dingtalk_identity.resolve_identity("code", settings=cfg)
    assert identity == dingtalk_identity.DingTalkIdentity("corp", "union", "member")
    assert seen == ["/v1.0/oauth2/userAccessToken", "/v1.0/contact/users/me",
                    "/v1.0/oauth2/accessToken", "/topapi/user/getbyunionid"]
    await dingtalk_identity.resolve_identity("second-code", settings=cfg)
    assert seen.count("/v1.0/oauth2/accessToken") == 1


@pytest.mark.asyncio
async def test_provider_rejects_external_contact(monkeypatch):
    dingtalk_identity._app_token_cache.clear()

    def handler(request: httpx.Request):
        if request.url.path.endswith("/userAccessToken"):
            return httpx.Response(200, json={"accessToken": "user-token", "corpId": "corp"})
        if request.url.path.endswith("/contact/users/me"):
            return httpx.Response(200, json={"unionId": "visitor"})
        if request.url.path.endswith("/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "app-token"})
        return httpx.Response(200, json={"errcode": 0,
                                         "result": {"contact_type": 1, "userid": "visitor"}})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(dingtalk_identity.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=transport, **kwargs))
    cfg = SimpleNamespace(
        dingtalk_client_id="app", dingtalk_client_secret="secret",
        dingtalk_corp_id="corp",
        dingtalk_redirect_uri="https://example.com/admin/user/dingtalk/callback",
    )
    with pytest.raises(dingtalk_identity.DingTalkAuthError):
        await dingtalk_identity.resolve_identity("code", settings=cfg)


@pytest.mark.asyncio
async def test_identity_binding_is_idempotent_and_unique_for_both_sides(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExternalIdentity.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def get_factory():
        return factory

    monkeypatch.setattr(external_identity, "init_async_session", get_factory)
    try:
        await external_identity.bind_identity("dingtalk", "corp", "union-1", "u1", "member-1")
        await external_identity.bind_identity("dingtalk", "corp", "union-1", "u1", "member-1")
        with pytest.raises(external_identity.IdentityConflictError):
            await external_identity.bind_identity("dingtalk", "corp", "union-1", "u2")
        with pytest.raises(external_identity.IdentityConflictError):
            await external_identity.bind_identity("dingtalk", "corp", "union-2", "u1")
        assert (await external_identity.find_identity("dingtalk", "corp", "union-1")).user_uid == "u1"
    finally:
        await engine.dispose()
