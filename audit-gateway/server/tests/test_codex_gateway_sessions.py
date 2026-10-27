import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from services.codex_channel import oauth
from utils.cache import CacheManager


class SharedRedis:
    def __init__(self): self.values = {}
    async def set(self, key, value, **kwargs):
        if key in self.values and kwargs.get("nx"): return False
        self.values[key] = value
        return True
    async def get(self, key): return self.values.get(key)
    async def getdel(self, key): return self.values.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    shared = SharedRedis()
    @asynccontextmanager
    async def connection(): yield shared
    monkeypatch.setattr(CacheManager, "get_instance", lambda: SimpleNamespace(redis_connection=connection))
    return shared


async def test_gateway_oauth_can_exchange_after_process_local_store_is_lost(redis, monkeypatch):
    sid, url = await oauth.start_gateway_login("admin")
    state = parse_qs(urlsplit(url).query)["state"][0]
    monkeypatch.setattr(oauth, "_sessions", oauth._SessionStore())
    exchange = AsyncMock(return_value=(oauth.TokenSet("at", "rt"), oauth.AccountIdentity()))
    monkeypatch.setattr(oauth, "exchange_code_with", exchange)
    await oauth.exchange_gateway_code("admin", sid, "http://localhost:1455/auth/callback?code=code&state=" + state)
    exchange.assert_awaited_once()
    with pytest.raises(oauth.OAuthError, match="不存在|已使用"):
        await oauth.exchange_gateway_code("admin", sid, "code=code&state=" + state)


async def test_foreign_admin_and_missing_or_wrong_state_cannot_consume_session(redis, monkeypatch):
    sid, url = await oauth.start_gateway_login("owner")
    state = parse_qs(urlsplit(url).query)["state"][0]
    for uid, callback in [("other", "code=x&state=" + state), ("owner", "code-only"), ("owner", "code=x&state=wrong")]:
        with pytest.raises(oauth.OAuthError): await oauth.exchange_gateway_code(uid, sid, callback)
        assert await redis.get("codex:oauth:" + sid)
    exchange = AsyncMock(return_value=(oauth.TokenSet("at", "rt"), oauth.AccountIdentity()))
    monkeypatch.setattr(oauth, "exchange_code_with", exchange)
    await oauth.exchange_gateway_code("owner", sid, "code=x&state=" + state)
    exchange.assert_awaited_once()


async def test_remote_redirect_is_rejected(redis):
    with pytest.raises(oauth.OAuthError): await oauth.start_gateway_login("admin", "https://foreign.example/auth/callback")
    assert redis.values == {}


async def test_existing_regular_provider_is_rejected_before_token_exchange(monkeypatch):
    from router.admin import codex
    from model.request.codex import CodexOAuthExchange, CodexProviderImport
    from utils.errors import ValidationError_
    monkeypatch.setattr(codex, "get_provider_by_code", AsyncMock(return_value=SimpleNamespace(base_url="https://ordinary.example/v1")))
    exchange = AsyncMock()
    refresh = AsyncMock()
    monkeypatch.setattr(oauth, "exchange_gateway_code", exchange)
    monkeypatch.setattr(codex, "refresh_tokens", refresh)
    operator = SimpleNamespace(uid="admin")
    with pytest.raises(ValidationError_):
        await codex.oauth_exchange(CodexOAuthExchange(session_id="sid", code="code=c&state=s", provider_code="ordinary"), operator)
    with pytest.raises(ValidationError_):
        await codex.provider_import(CodexProviderImport(provider_code="ordinary", refresh_token="fake-test"), operator)
    exchange.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.parametrize("permanent, expected_status, expected_code", [(True, 503, "codex_login_required"), (False, 502, "codex_refresh_unavailable")])
async def test_refresh_failure_does_not_return_employee_authentication_401(monkeypatch, permanent, expected_status, expected_code):
    from services import forwarding
    from services.codex_channel import credentials
    error = oauth.OAuthLoginRequired("expired") if permanent else oauth.OAuthError("temporary")
    import services.codex_channel as channel
    monkeypatch.setattr(channel, "ensure_fresh", AsyncMock(side_effect=error))
    target = SimpleNamespace(provider_id=9, api_key=credentials.CodexCredential(oauth.TokenSet("at", "rt")).to_json())
    with pytest.raises(forwarding.UpstreamError) as raised:
        await forwarding._codex_credential(target)
    assert raised.value.status_code == expected_status
    assert raised.value.code == expected_code
