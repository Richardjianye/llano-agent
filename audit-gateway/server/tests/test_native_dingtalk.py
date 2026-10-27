"""本地工作台交接只在正确 PKCE 和活跃内部账号下签发令牌。"""
import base64
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from router.admin import dingtalk, native_login
from services.dingtalk_identity import DingTalkIdentity


class Cache:
    def __init__(self): self.values = {}
    @asynccontextmanager
    async def redis_connection(self): yield self
    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values: return False
        self.values[key] = value
        return True
    async def getdel(self, key): return self.values.pop(key, None)


@pytest.fixture
def browser(monkeypatch):
    cfg = SimpleNamespace(is_prod=False, dingtalk_corp_id="corp")
    cache = Cache()
    monkeypatch.setattr(dingtalk.CacheManager, "get_instance", lambda: cache)
    monkeypatch.setattr(dingtalk, "get_settings", lambda: cfg)
    monkeypatch.setattr(native_login, "get_settings", lambda: cfg)
    monkeypatch.setattr(dingtalk, "configured", lambda: True)
    monkeypatch.setattr(dingtalk, "authorization_url", lambda state: "https://login.dingtalk.com/test?state=" + state)
    monkeypatch.setattr(dingtalk, "resolve_identity", AsyncMock(return_value=DingTalkIdentity("corp", "union", "member")))
    monkeypatch.setattr(dingtalk, "find_identity", AsyncMock(return_value=SimpleNamespace(user_uid="local")))
    user = SimpleNamespace(uid="local", username="花名", status=1, account_kind="internal")
    monkeypatch.setattr(dingtalk, "get_user_info", AsyncMock(return_value=user))
    monkeypatch.setattr(native_login, "get_user_info", AsyncMock(return_value=user))
    minted = AsyncMock(return_value=("access-secret", "refresh-secret"))
    monkeypatch.setattr(native_login, "mint_user_tokens", minted)
    app = FastAPI()
    app.include_router(dingtalk.router, prefix="/admin")
    app.include_router(native_login.router, prefix="/admin")
    with TestClient(app, follow_redirects=False) as client:
        yield client, user, minted


VERIFIER = "v" * 43
STATE = "s" * 32
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def authorize(client, redirect="http://127.0.0.1:51320/callback"):
    return client.get("/admin/auth/native/authorize", params={
        "redirect_uri": redirect, "state": STATE, "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "provider": "dingtalk",
    })


def handoff(client):
    start = authorize(client)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    completed = client.get("/admin/user/dingtalk/callback", params={"state": state, "code": "ding-code"})
    assert "access-secret" not in completed.headers["location"]
    assert "access_token=" not in completed.headers.get("set-cookie", "")
    query = parse_qs(urlparse(completed.headers["location"]).query)
    assert query["state"] == [STATE]
    return query["code"][0]


@pytest.mark.parametrize("redirect", ["https://evil.com/callback", "http://localhost:8000/callback", "http://127.0.0.1:9000/callback?x=1", "http://name@127.0.0.1:8000/callback"])
def test_rejects_open_redirects(browser, redirect):
    client, _, minted = browser
    assert authorize(client, redirect).status_code == 400
    minted.assert_not_awaited()


def test_native_code_is_one_time_and_pkce_required(browser):
    client, user, minted = browser
    code = handoff(client)
    minted.assert_not_awaited()
    response = client.post("/admin/auth/native/token", json={"code": code, "code_verifier": VERIFIER})
    assert response.json()["access_token"] == "access-secret"
    assert response.json()["user_id"] == user.uid
    assert response.json()["username"] == user.username
    assert user.uid != user.username
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/admin/auth/native/token", json={"code": code, "code_verifier": VERIFIER}).status_code == 401
    minted.assert_awaited_once()


def test_wrong_verifier_consumes_code_without_minting(browser):
    client, _, minted = browser
    code = handoff(client)
    response = client.post("/admin/auth/native/token", json={"code": code, "code_verifier": "wrong" * 10})
    assert response.status_code == 401
    assert client.post("/admin/auth/native/token", json={"code": code, "code_verifier": VERIFIER}).status_code == 401
    minted.assert_not_awaited()


@pytest.mark.parametrize("status,kind", [(0, "internal"), (1, "external")])
def test_exchange_rechecks_disabled_or_external_account(browser, status, kind):
    client, user, minted = browser
    code = handoff(client)
    user.status, user.account_kind = status, kind
    assert client.post("/admin/auth/native/token", json={"code": code, "code_verifier": VERIFIER}).status_code == 403
    minted.assert_not_awaited()
