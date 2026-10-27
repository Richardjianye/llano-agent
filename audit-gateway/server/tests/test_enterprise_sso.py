"""真实 ASGI 路由 + SQLite 应用登记 + 有状态授权码的 OIDC 链路。"""

import asyncio
import base64
import hashlib
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from jose import jwt
from redis import asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import SSOClient
from model.response import UserInfo
from router import sso
from services import enterprise_sso
from utils.crypt import JWTAuth
from utils.dependencies import require_login
from utils.errors import PermissionError_

SESSION_TOKEN = jwt.encode({"iat": 1_700_000_000, "jti": "browser-session"}, "test-session", algorithm="HS256")


def test_workspace_jwt_carries_authentication_time_for_oidc():
    user = SimpleNamespace(uid="uid-1", nickname="员工", permission="employee")
    access = JWTAuth().create_access_token(user, auth_time=1_700_000_000)
    refresh = JWTAuth().create_refresh_token(user, auth_time=1_700_000_000)
    assert jwt.get_unverified_claims(access)["auth_time"] == 1_700_000_000
    assert jwt.get_unverified_claims(refresh)["auth_time"] == 1_700_000_000


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def smembers(self, key):
        return self.sets.get(key, set())

    async def expire(self, key, ttl):
        return True

    async def incr(self, key):
        self.values[key] = int(self.values.get(key) or 0) + 1
        return self.values[key]

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.sets.pop(key, None)


@pytest.fixture
async def oidc(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SSOClient.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_maker():
        return maker

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    cfg = SimpleNamespace(sso_issuer="http://localhost", sso_org_id="corp-1",
                          sso_signing_private_key=pem, sso_signing_key_id="test-key", is_prod=False)
    monkeypatch.setattr(enterprise_sso, "get_settings", lambda: cfg)
    monkeypatch.setattr(sso, "get_settings", lambda: cfg)
    monkeypatch.setattr(enterprise_sso, "init_async_session", session_maker)
    monkeypatch.setattr(sso, "init_async_session", session_maker)

    redis = FakeRedis()
    redis.values["active_token:browser-session"] = "active"

    @asynccontextmanager
    async def redis_connection():
        yield redis

    monkeypatch.setattr(enterprise_sso.CacheManager, "get_instance",
                        lambda: SimpleNamespace(redis_connection=redis_connection))

    user = SimpleNamespace(uid="uid-1", status=1, account_kind="internal", username="员工",
                           realname="某员工", nickname="员工")

    async def get_user_info(*, uid):
        return user if uid == user.uid else None

    async def current_user(*, token, request):
        return UserInfo(id=1, uid=user.uid, permission="employee") if token == SESSION_TOKEN else sso.GatewayResponse(code=10002)

    async def to_user_info(_user):
        return UserInfo(id=1, uid=user.uid, permission="employee", dept_code="sales")

    monkeypatch.setattr(sso, "get_user_info", get_user_info)
    monkeypatch.setattr(sso, "get_current_user", current_user)
    monkeypatch.setattr(sso, "to_user_info", to_user_info)

    app = FastAPI()
    app.include_router(sso.router)
    app.include_router(sso.admin_router)
    operator_role = {"value": "president"}
    app.dependency_overrides[require_login] = lambda: UserInfo(
        id=2, uid="admin", permission=operator_role["value"])

    @app.exception_handler(enterprise_sso.SSOError)
    async def sso_error(_request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.exception_handler(PermissionError_)
    async def permission_error(_request, _exc):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        yield client, cfg, redis, user, operator_role
    await engine.dispose()


async def register(client):
    result = await client.post("/admin/sso/clients", json={
        "client_id": "artifact", "name": "用户产物系统", "owner_uid": "uid-1",
        "environment": "dev", "audit_contact": "security@example.test",
        "client_type": "confidential", "redirect_uris": ["http://127.0.0.1:8765/callback"],
    })
    assert result.status_code == 200, result.text
    return result.json()["client_secret"]


@pytest.mark.asyncio
async def test_developer_can_manage_sso_client_but_auditor_cannot(oidc):
    client, _, _, _, operator_role = oidc
    operator_role["value"] = "developer"
    secret = await register(client)
    assert secret
    update = await client.put("/admin/sso/clients/artifact", json={
        "name": "推广中台", "owner_uid": "uid-1", "audit_contact": "security@example.test",
        "redirect_uris": ["https://dashboard.example.test/api/sso/callback"],
        "allowed_scopes": ["openid", "profile", "org", "directory.read"],
    })
    assert update.status_code == 200, update.text
    rotated = await client.post("/admin/sso/clients/artifact/rotate-secret")
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["client_secret"] != secret
    assert (await client.post("/admin/sso/clients/artifact/disable")).status_code == 200
    assert (await client.post("/admin/sso/clients/artifact/enable")).status_code == 200

    operator_role["value"] = "auditor"
    denied = await client.post("/admin/sso/clients", json={
        "client_id": "blocked", "name": "不应登记", "owner_uid": "uid-1",
        "environment": "dev", "audit_contact": "security@example.test",
        "client_type": "confidential",
        "redirect_uris": ["https://dashboard.example.test/api/sso/callback"],
    })
    assert denied.status_code == 403
    assert (await client.post("/admin/sso/clients/artifact/rotate-secret")).status_code == 403


def authorize_params():
    verifier = "A" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, {"response_type": "code", "client_id": "artifact",
                      "redirect_uri": "http://127.0.0.1:8765/callback", "scope": "openid profile org",
                      "state": "s" * 32, "nonce": "n" * 32,
                      "code_challenge": challenge, "code_challenge_method": "S256"}


@pytest.mark.asyncio
async def test_oidc_code_token_userinfo_revocation_and_account_disable(oidc):
    client, cfg, redis, user, _ = oidc
    secret = await register(client)
    verifier, params = authorize_params()
    client.cookies.set("access_token", SESSION_TOKEN)
    response = await client.get("/sso/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(params["redirect_uri"])
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": params["redirect_uri"], "code_verifier": verifier,
            "client_id": "artifact", "client_secret": secret}
    exchange = await client.post("/sso/token", data=form)
    assert exchange.status_code == 200, exchange.text
    assert exchange.headers["cache-control"] == "no-store"
    issued = exchange.json()
    claims = jwt.decode(issued["id_token"], (await client.get("/sso/jwks")).json()["keys"][0],
                        algorithms=["RS256"], audience="artifact", issuer=cfg.sso_issuer)
    assert claims["sub"] == "uid-1" and claims["dept_code"] == "sales"
    assert claims["nonce"] == params["nonce"]
    assert (await client.post("/sso/token", data=form)).json()["error"] == "invalid_grant"
    bearer = {"Authorization": "Bearer " + issued["access_token"]}
    assert (await client.get("/sso/userinfo", headers=bearer)).json()["sub"] == "uid-1"
    user.status = 0
    assert (await client.get("/sso/userinfo", headers=bearer)).status_code == 401
    user.status = 1
    await enterprise_sso.revoke_user_sessions(user.uid)
    assert (await client.get("/sso/userinfo", headers=bearer)).status_code == 401


@pytest.mark.asyncio
async def test_oidc_rejects_unregistered_redirect_and_wrong_pkce(oidc):
    client, _, _, _, _ = oidc
    secret = await register(client)
    verifier, params = authorize_params()
    client.cookies.set("access_token", SESSION_TOKEN)
    wrong = await client.get("/sso/authorize", params={**params, "redirect_uri": "http://127.0.0.1:8765/other"})
    assert wrong.status_code == 400
    client.cookies.clear()
    login = await client.get("/sso/authorize", params=params, follow_redirects=False)
    assert login.status_code == 303
    assert parse_qs(urlparse(login.headers["location"]).query)["redirect"][0].startswith("/sso/authorize?")
    client.cookies.set("access_token", SESSION_TOKEN)
    response = await client.get("/sso/authorize", params=params, follow_redirects=False)
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": params["redirect_uri"],
            "code_verifier": "B" * 43, "client_id": "artifact", "client_secret": secret}
    assert (await client.post("/sso/token", data=form)).json()["error"] == "invalid_grant"
    assert (await client.post("/sso/token", data={**form, "code_verifier": verifier})).json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_oidc_logout_invalidates_pending_code_and_client_secret_is_required(oidc):
    client, _, _, user, _ = oidc
    secret = await register(client)
    verifier, params = authorize_params()
    client.cookies.set("access_token", SESSION_TOKEN)
    response = await client.get("/sso/authorize", params=params, follow_redirects=False)
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": params["redirect_uri"], "code_verifier": verifier,
            "client_id": "artifact"}
    assert (await client.post("/sso/token", data={**form, "client_secret": "wrong"})).status_code == 401
    await enterprise_sso.revoke_user_sessions(user.uid)
    assert (await client.post("/sso/token", data={**form, "client_secret": secret})).json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_client_secret_rotation_and_jwks_grace(oidc, tmp_path):
    client, cfg, _, _, _ = oidc
    old_secret = await register(client)
    listed = (await client.get("/admin/sso/clients")).json()
    assert listed[0]["client_id"] == "artifact"
    assert "secret_hash" not in listed[0]
    rotated = await client.post("/admin/sso/clients/artifact/rotate-secret")
    assert rotated.status_code == 200
    new_secret = rotated.json()["client_secret"]
    assert new_secret != old_secret
    verifier, params = authorize_params()
    form = {"grant_type": "authorization_code", "code": "missing",
            "redirect_uri": params["redirect_uri"], "code_verifier": verifier,
            "client_id": "artifact"}
    assert (await client.post("/sso/token", data={**form, "client_secret": old_secret})).json()["error"] == "invalid_client"
    assert (await client.post("/sso/token", data={**form, "client_secret": new_secret})).json()["error"] == "invalid_grant"
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "prior-key.pem").write_bytes(old_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    cfg.sso_public_key_archive_dir = str(tmp_path)
    keys = (await client.get("/sso/jwks")).json()["keys"]
    assert {key["kid"] for key in keys} == {"test-key", "prior-key"}


@pytest.mark.asyncio
async def test_production_app_mounts_sso_and_fails_closed_when_unconfigured(monkeypatch):
    from main import create_app

    cfg = SimpleNamespace(sso_issuer=None, sso_org_id=None, sso_signing_private_key=None,
                          sso_signing_key_id=None, is_prod=False)
    monkeypatch.setattr(enterprise_sso, "get_settings", lambda: cfg)
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/.well-known/openid-configuration")
    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"


@pytest.mark.asyncio
async def test_oidc_real_redis_code_replay_and_revocation(oidc, monkeypatch, tmp_path):
    """在独立 Redis 进程上走真实 ASGI 授权、兑换与撤销链路。"""
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is not installed")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [executable, "--bind", "127.0.0.1", "--port", str(port),
         "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    redis = aioredis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        for _ in range(50):
            if process.poll() is not None:
                pytest.fail("isolated redis-server exited before readiness")
            try:
                await redis.ping()
                break
            except (OSError, RedisConnectionError):
                await asyncio.sleep(0.02)
        else:
            pytest.fail("isolated redis-server did not become ready")

        @asynccontextmanager
        async def redis_connection():
            yield redis

        monkeypatch.setattr(enterprise_sso.CacheManager, "get_instance",
                            lambda: SimpleNamespace(redis_connection=redis_connection))
        await redis.set("active_token:browser-session", "active", ex=300)
        client, cfg, _, user, _ = oidc
        secret = await register(client)
        verifier, params = authorize_params()
        client.cookies.set("access_token", SESSION_TOKEN)
        response = await client.get("/sso/authorize", params=params, follow_redirects=False)
        assert response.status_code == 303
        code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": params["redirect_uri"], "code_verifier": verifier,
                "client_id": "artifact", "client_secret": secret}
        issued = await client.post("/sso/token", data=form)
        assert issued.status_code == 200, issued.text
        assert (await client.post("/sso/token", data=form)).json()["error"] == "invalid_grant"
        access = issued.json()["access_token"]
        bearer = {"Authorization": "Bearer " + access}
        assert (await client.get("/sso/userinfo", headers=bearer)).json()["sub"] == user.uid
        assert await redis.ttl("sso:access:" + hashlib.sha256(access.encode()).hexdigest()) > 0
        await enterprise_sso.revoke_user_sessions(user.uid)
        assert (await client.get("/sso/userinfo", headers=bearer)).status_code == 401

        second = await client.get("/sso/authorize", params=params, follow_redirects=False)
        second_code = parse_qs(urlparse(second.headers["location"]).query)["code"][0]
        await enterprise_sso.revoke_user_sessions(user.uid)
        assert (await client.post("/sso/token", data={**form, "code": second_code})).json()["error"] == "invalid_grant"
    finally:
        await redis.aclose()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


@pytest.mark.asyncio
async def test_authorize_errors_return_to_registered_redirect_only(oidc):
    client, _, _, user, _ = oidc
    await register(client)
    _, params = authorize_params()
    client.cookies.set("access_token", SESSION_TOKEN)

    def redirected_error(response):
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith(params["redirect_uri"] + "?")
        query = parse_qs(urlparse(location).query)
        assert query["state"] == [params["state"]]
        assert "code" not in query
        return query["error"][0]

    assert redirected_error(await client.get("/sso/authorize", params={**params, "scope": "openid email"})) == "invalid_scope"
    assert redirected_error(await client.get("/sso/authorize", params={**params, "response_type": "token"})) == "unsupported_response_type"
    assert redirected_error(await client.get("/sso/authorize", params={**params, "code_challenge_method": "plain"})) == "invalid_request"
    user.status = 0
    assert redirected_error(await client.get("/sso/authorize", params=params)) == "access_denied"
    user.status = 1
    client.cookies.clear()
    assert redirected_error(await client.get("/sso/authorize", params={**params, "prompt": "none"})) == "login_required"

    # 回调或应用未核验时绝不回跳，避免开放重定向。
    unknown = await client.get("/sso/authorize", params={**params, "redirect_uri": "https://evil.example/cb"})
    assert unknown.status_code == 400 and "location" not in unknown.headers
    missing = await client.get("/sso/authorize", params={**params, "client_id": "nobody"})
    assert missing.status_code == 400 and "location" not in missing.headers


@pytest.mark.asyncio
async def test_token_endpoint_rejects_unknown_or_disabled_client_as_unauthorized(oidc):
    client, _, _, _, _ = oidc
    secret = await register(client)
    verifier, params = authorize_params()
    form = {"grant_type": "authorization_code", "code": "missing", "redirect_uri": params["redirect_uri"],
            "code_verifier": verifier, "client_id": "nobody", "client_secret": secret}
    unknown = await client.post("/sso/token", data=form)
    assert (unknown.status_code, unknown.json()["error"]) == (401, "invalid_client")
    client.cookies.set("access_token", SESSION_TOKEN)
    response = await client.get("/sso/authorize", params=params, follow_redirects=False)
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    issued = (await client.post("/sso/token", data={**form, "client_id": "artifact", "code": code})).json()
    assert (await client.post("/admin/sso/clients/artifact/disable")).status_code == 200
    disabled = await client.post("/sso/token", data={**form, "client_id": "artifact"})
    assert (disabled.status_code, disabled.json()["error"]) == (401, "invalid_client")
    bearer = {"Authorization": "Bearer " + issued["access_token"]}
    assert (await client.get("/sso/userinfo", headers=bearer)).status_code == 401


@pytest.mark.asyncio
async def test_admin_can_update_redirects_and_reenable_client(oidc):
    client, _, _, _, _ = oidc
    await register(client)
    _, params = authorize_params()
    update = {"name": "产物系统", "owner_uid": "uid-1", "audit_contact": "sec@example.test",
              "redirect_uris": ["http://127.0.0.1:9000/cb"], "allowed_scopes": ["openid"]}
    rejected = await client.put("/admin/sso/clients/artifact", json={**update, "redirect_uris": ["ftp://x/cb"]})
    assert rejected.status_code == 400
    assert (await client.put("/admin/sso/clients/artifact", json=update)).status_code == 200
    listed = (await client.get("/admin/sso/clients")).json()[0]
    assert listed["redirect_uris"] == update["redirect_uris"] and listed["allowed_scopes"] == ["openid"]
    client.cookies.set("access_token", SESSION_TOKEN)
    old = await client.get("/sso/authorize", params=params)
    assert old.status_code == 400
    moved = await client.get("/sso/authorize", params={**params, "redirect_uri": "http://127.0.0.1:9000/cb",
                                                         "scope": "openid"})
    assert moved.status_code == 303 and "code=" in moved.headers["location"]

    await client.post("/admin/sso/clients/artifact/disable")
    assert (await client.get("/sso/authorize", params=params)).status_code == 400
    assert (await client.post("/admin/sso/clients/artifact/enable")).json()["enabled"] is True
    assert (await client.get("/admin/sso/clients")).json()[0]["enabled"] is True


@pytest.mark.asyncio
async def test_registration_explains_environment_mismatch(oidc):
    client, _, _, _, _ = oidc
    result = await client.post("/admin/sso/clients", json={
        "client_id": "prod-app", "name": "正式应用", "owner_uid": "uid-1", "environment": "prod",
        "audit_contact": "sec@example.test", "client_type": "public",
        "redirect_uris": ["https://app.example/cb"],
    })
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_directory_scope_can_be_registered_but_not_used_for_oidc_authorization(oidc):
    client, _, _, _, _ = oidc
    await register(client)
    update = {"name": "推广看板", "owner_uid": "uid-1", "audit_contact": "security@example.test",
              "redirect_uris": ["http://127.0.0.1:8765/callback"],
              "allowed_scopes": ["openid", "profile", "org", "directory.read"]}
    result = await client.put("/admin/sso/clients/artifact", json=update)
    assert result.status_code == 200, result.text
    listed = (await client.get("/admin/sso/clients")).json()[0]
    assert "directory.read" in listed["allowed_scopes"]
    client.cookies.set("access_token", SESSION_TOKEN)
    _, params = authorize_params()
    response = await client.get("/sso/authorize", params={**params, "scope": "openid directory.read"})
    assert response.status_code == 303
    assert parse_qs(urlparse(response.headers["location"]).query)["error"] == ["invalid_scope"]
