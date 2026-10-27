import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db.updates import user as user_updates
from model.database import User
from model.response import UserInfo
from router.admin import account, user
from router.admin.apikey import ApiKeyCreate
from router.v1 import chat
from utils import dependencies, gateway_auth
from utils.errors import ApiKeyAuthError, AuthError
from utils.gateway_auth import require_caller


def test_api_key_create_accepts_custom_expiry_and_independent_quota():
    body = ApiKeyCreate(
        name="temporary client",
        expires_at="2099-01-01T12:30",
        total_token_limit=1_000_000,
    )
    assert body.expires_at is not None
    assert body.total_token_limit == 1_000_000


def test_api_key_create_rejects_non_positive_quota():
    with pytest.raises(ValueError):
        ApiKeyCreate(name="invalid", total_token_limit=0)


class _Redis:
    delete = AsyncMock()


class _RedisContext:
    async def __aenter__(self):
        return _Redis()

    async def __aexit__(self, *_args):
        return False


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _RevocationState:
    """测试用最小有状态吊销器，让管理动作与 `/v1` 校验走同一份状态。"""

    def __init__(self, token_users=None):
        self.token_users = dict(token_users or {})
        self.revoked = set()

    async def revoke_token(self, token: str, reason: str = "logout"):
        self.revoked.add(token)
        return True

    async def revoke_user_tokens(self, uid: str, reason: str = "security"):
        matched = {
            token for token, token_uid in self.token_users.items() if token_uid == uid
        }
        self.revoked.update(matched)
        return len(matched)

    async def is_token_revoked(self, token: str):
        return token in self.revoked


def _valid_user(uid: str = "u1") -> UserInfo:
    return UserInfo(
        id=1,
        uid=uid,
        username="developer",
        permission="developer",
        dept_id=7,
        dept_code="exec",
    )


@pytest.mark.asyncio
async def test_desktop_logout_revokes_refresh_token_from_request_body(monkeypatch):
    revoked = AsyncMock(return_value=True)
    monkeypatch.setattr(user.token_manager, "revoke_token", revoked)
    monkeypatch.setattr(
        user.CacheManager,
        "get_instance",
        lambda: SimpleNamespace(redis_connection=lambda: _RedisContext()),
    )
    request = SimpleNamespace(
        cookies={}, headers={"Authorization": "Bearer access-token"}
    )

    await user.user_logout(
        request, user.LogoutTokens(refresh_token="refresh-token")
    )

    assert revoked.await_args_list == [
        (("access-token",), {"reason": "logout"}),
        (("refresh-token",), {"reason": "logout"}),
    ]


@pytest.mark.asyncio
async def test_logged_out_jwt_is_rejected_by_v1_caller_auth(monkeypatch):
    state = _RevocationState({"access-token": "u1"})
    monkeypatch.setattr(user, "token_manager", state)
    monkeypatch.setattr(gateway_auth, "token_manager", state)
    check_token = AsyncMock(return_value=_valid_user())
    monkeypatch.setattr(gateway_auth, "check_token", check_token)
    monkeypatch.setattr(
        user.CacheManager,
        "get_instance",
        lambda: SimpleNamespace(redis_connection=lambda: _RedisContext()),
    )
    request = SimpleNamespace(
        cookies={}, headers={"Authorization": "Bearer access-token"}
    )

    await user.user_logout(request, user.LogoutTokens())

    with pytest.raises(AuthError, match="身份凭证已失效") as caught:
        await require_caller(request)
    assert caught.value.code == 10002
    check_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_reset_jwt_is_rejected_by_v1_caller_auth(monkeypatch):
    state = _RevocationState({"old-access-token": "u1"})
    monkeypatch.setattr(account, "token_manager", state)
    monkeypatch.setattr(gateway_auth, "token_manager", state)
    check_token = AsyncMock(return_value=_valid_user())
    monkeypatch.setattr(gateway_auth, "check_token", check_token)
    monkeypatch.setattr(
        account,
        "_load_target",
        AsyncMock(
            return_value=SimpleNamespace(
                uid="u1",
                username="developer",
                realname="Developer",
                permission="developer",
            )
        ),
    )
    monkeypatch.setattr(account, "hash_password_async", AsyncMock(return_value="hash"))
    monkeypatch.setattr(account, "update_user_password", AsyncMock())
    monkeypatch.setattr(account, "revoke_user_api_keys", AsyncMock())

    await account.account_reset_password(
        account.AccountPasswordReset(uid="u1", password="new-secret-123"),
        SimpleNamespace(uid="president", permission="president"),
    )

    request = SimpleNamespace(
        cookies={}, headers={"Authorization": "Bearer old-access-token"}
    )
    with pytest.raises(AuthError, match="身份凭证已失效") as caught:
        await require_caller(request)
    assert caught.value.code == 10002
    check_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_key_can_revoke_itself_even_after_account_is_disabled(monkeypatch):
    record = SimpleNamespace(
        id=7,
        key_hash="stored-hash",
        key_prefix="sk-llano-prefix",
        status=1,
        user_uid="u1",
    )
    revoked = AsyncMock()
    monkeypatch.setattr(chat, "get_key_by_prefix", AsyncMock(return_value=record))
    monkeypatch.setattr(chat, "key_prefix_of", lambda _plain: record.key_prefix)
    monkeypatch.setattr(chat, "verify_api_key", lambda _plain, _hash: True)
    monkeypatch.setattr(chat, "revoke_api_key", revoked)
    request = SimpleNamespace(
        headers={"Authorization": "Bearer sk-llano-device-secret"}
    )

    response = await chat.revoke_calling_device(request)

    assert response == {"revoked": True}
    revoked.assert_awaited_once_with(7, reason="client_logout")


@pytest.mark.asyncio
async def test_v1_missing_credential_is_an_http_401_auth_error():
    request = SimpleNamespace(headers={}, cookies={})

    with pytest.raises(ApiKeyAuthError) as caught:
        await require_caller(request)

    assert caught.value.http_status == 401


@pytest.mark.asyncio
async def test_v1_rejects_revoked_browser_jwt_before_loading_user(monkeypatch):
    check = AsyncMock()
    monkeypatch.setattr(gateway_auth, "check_token", check)
    monkeypatch.setattr(gateway_auth, "token_manager", SimpleNamespace(
        is_token_revoked=AsyncMock(return_value=True)))
    request = SimpleNamespace(headers={"Authorization": "Bearer revoked-jwt"}, cookies={})

    with pytest.raises(gateway_auth.AuthError):
        await require_caller(request)
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_account_revokes_tokens_and_all_device_keys(monkeypatch):
    operator = SimpleNamespace(uid="admin", permission="president")
    target = SimpleNamespace(
        uid="u1", username="employee", permission="employee"
    )
    monkeypatch.setattr(account, "_load_target", AsyncMock(return_value=target))
    update_status = AsyncMock()
    revoke_tokens = AsyncMock(return_value=2)
    revoke_keys = AsyncMock()
    monkeypatch.setattr(account, "update_user_status", update_status)
    monkeypatch.setattr(account.token_manager, "revoke_user_tokens", revoke_tokens)
    monkeypatch.setattr(account, "revoke_user_api_keys", revoke_keys)

    response = await account.account_status(
        account.AccountStatus(uid="u1", status=0), operator
    )

    update_status.assert_awaited_once_with("u1", 0)
    revoke_tokens.assert_awaited_once_with("u1", reason="account_disabled")
    revoke_keys.assert_awaited_once_with("u1", reason="account_disabled", by="admin")
    assert "重新启用不会自动恢复" in response.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assignment",
    [
        account.AccountAssign(uid="u1", permission="employee"),
        account.AccountAssign(uid="u1", dept_id=8),
    ],
)
async def test_role_or_department_assignment_revokes_existing_sessions(
    monkeypatch, assignment
):
    target = SimpleNamespace(
        uid="u1",
        username="manager",
        realname="Manager",
        permission="manager",
        dept_id=7,
    )
    revoke_tokens = AsyncMock(return_value=2)
    monkeypatch.setattr(account, "_load_target", AsyncMock(return_value=target))
    monkeypatch.setattr(account, "get_department", AsyncMock(return_value=object()))
    monkeypatch.setattr(account, "update_user_info", AsyncMock())
    monkeypatch.setattr(
        account,
        "token_manager",
        SimpleNamespace(revoke_user_tokens=revoke_tokens),
    )

    response = await account.account_assign(
        assignment,
        SimpleNamespace(uid="president", permission="president"),
    )

    revoke_tokens.assert_awaited_once_with(
        "u1", reason="account_assignment_changed"
    )
    assert "需重新登录" in response.message


@pytest.mark.asyncio
async def test_explicit_null_department_clears_department_and_revokes_sessions(
    monkeypatch,
):
    target = SimpleNamespace(
        uid="u1",
        username="manager",
        realname="Manager",
        permission="manager",
        dept_id=7,
    )
    update_user = AsyncMock()
    revoke_tokens = AsyncMock(return_value=1)
    get_department = AsyncMock()
    monkeypatch.setattr(account, "_load_target", AsyncMock(return_value=target))
    monkeypatch.setattr(account, "get_department", get_department)
    monkeypatch.setattr(account, "update_user_info", update_user)
    monkeypatch.setattr(
        account,
        "token_manager",
        SimpleNamespace(revoke_user_tokens=revoke_tokens),
    )

    response = await account.account_assign(
        account.AccountAssign(uid="u1", dept_id=None),
        SimpleNamespace(uid="president", permission="president"),
    )

    get_department.assert_not_awaited()
    update_user.assert_awaited_once()
    updated_user = update_user.await_args.args[0]
    assert updated_user.dept_id is None
    assert update_user.await_args.kwargs == {"fields": {"dept_id"}}
    revoke_tokens.assert_awaited_once_with(
        "u1", reason="account_assignment_changed"
    )
    assert "需重新登录" in response.message


@pytest.mark.asyncio
async def test_update_user_info_persists_explicit_null_department(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    session_maker = lambda: _AsyncContext(session)
    monkeypatch.setattr(
        user_updates,
        "init_async_session",
        AsyncMock(return_value=session_maker),
    )

    await user_updates.update_user_info(
        User(uid="u1", dept_id=None), fields={"dept_id"}
    )

    statement = session.execute.await_args.args[0]
    assert statement.compile().params["dept_id"] is None
    session.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_profile_only_assignment_does_not_revoke_sessions(monkeypatch):
    target = SimpleNamespace(
        uid="u1",
        username="manager",
        realname="Manager",
        permission="manager",
        dept_id=7,
    )
    revoke_tokens = AsyncMock(return_value=2)
    monkeypatch.setattr(account, "_load_target", AsyncMock(return_value=target))
    monkeypatch.setattr(account, "update_user_info", AsyncMock())
    monkeypatch.setattr(
        account,
        "token_manager",
        SimpleNamespace(revoke_user_tokens=revoke_tokens),
    )

    response = await account.account_assign(
        account.AccountAssign(uid="u1", realname="New Name"),
        SimpleNamespace(uid="president", permission="president"),
    )

    revoke_tokens.assert_not_awaited()
    assert response.message == "调整成功"


@pytest.mark.asyncio
async def test_user_context_contains_server_permissions_and_default_route(monkeypatch):
    operator = SimpleNamespace(
        id=1,
        uid="president-1",
        username="总裁",
        nickname=None,
        realname="测试总裁",
        status=1,
        permission="president",
        dept_id=7,
    )
    monkeypatch.setattr(
        "db.selects.department.get_department",
        AsyncMock(return_value=SimpleNamespace(name="管理部门", code="exec")),
    )

    info = await dependencies.to_user_info(operator, exp=1_800_000_000)

    assert info.permissions == [
        "dept:view",
        "dept:edit",
        "user:view",
        "user:edit",
        "model:view",
        "model:edit",
        "member:search",
        "apikey:admin",
        "mcp:view",
        "mcp:edit",
        "release:view",
        "release:edit",
        "usage:view",
        "usage:view:all",
        "skill_security:view",
        "skill_security:sample",
        "skill_hub:view",
        "skill_hub:promote",
        "skill_hub:edit",
        "redaction:edit",
        "prompt_policy:publish",
        "prompt_policy:view",
        "audit:view",
    ]
    assert info.default_route == "dashboard"


@pytest.mark.asyncio
async def test_login_returns_tokens_and_user_context_in_one_response(monkeypatch):
    operator = SimpleNamespace(
        id=1,
        uid="president-1",
        username="总裁",
        nickname=None,
        realname="测试总裁",
        password="hashed",
        status=1,
        permission="president",
        dept_id=None,
    )
    monkeypatch.setattr(user, "get_user_by_username", AsyncMock(return_value=operator))
    monkeypatch.setattr(user, "check_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(user.jwt_auth, "create_access_token", lambda _user: "access")
    monkeypatch.setattr(user.jwt_auth, "create_refresh_token", lambda _user: "refresh")
    monkeypatch.setattr(user.token_manager, "store_token", AsyncMock())
    monkeypatch.setattr(user, "update_user_last_login", AsyncMock())

    response = await user.user_login(
        user.UserLogin(username="总裁", password="secret"),
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
    )
    payload = json.loads(response.body)

    assert payload["data"]["access_token"] == "access"
    assert payload["data"]["refresh_token"] == "refresh"
    assert payload["data"]["user"]["uid"] == "president-1"
    assert payload["data"]["user"]["default_route"] == "dashboard"


def test_auth_cookies_follow_token_lifetimes_and_are_secure_in_production():
    response = user.FastAPIResponse()
    settings = SimpleNamespace(
        access_token_expire_minutes=15,
        refresh_token_expire_minutes=2_880,
        is_prod=True,
    )

    user._set_auth_cookies(
        response,
        access_token="access",
        refresh_token="refresh",
        settings=settings,
    )
    cookies = response.headers.getlist("set-cookie")

    assert any(
        "access_token=access" in cookie
        and "Max-Age=900" in cookie
        and "HttpOnly" in cookie
        and "SameSite=lax" in cookie
        and "Secure" in cookie
        for cookie in cookies
    )
    assert any(
        "refresh_token=refresh" in cookie
        and "Max-Age=172800" in cookie
        and "HttpOnly" in cookie
        and "SameSite=lax" in cookie
        and "Secure" in cookie
        for cookie in cookies
    )
