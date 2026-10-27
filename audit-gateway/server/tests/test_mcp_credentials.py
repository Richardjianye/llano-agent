"""每用户 MCP 上游凭据的输入、所有权、加密与审计边界。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from model.database import (CredentialAccessLog, McpInstance,
                            PlatformCredential)
from model.request.mcp_credential import McpCredentialBind
from utils.gateway_auth import Caller
from utils.errors import McpUpstreamError, PermissionError_


def test_bind_request_accepts_only_account_and_password():
    body = McpCredentialBind(account="alice", password="private-password")

    assert body.model_dump() == {
        "account": "alice",
        "password": "private-password",
    }
    with pytest.raises(ValidationError):
        McpCredentialBind.model_validate({
            "account": "alice",
            "password": "private-password",
            "user_uid": "victim-uid",
        })


@pytest.mark.parametrize(
    "payload",
    [
        {"account": "", "password": "private-password"},
        {"account": "alice", "password": ""},
        {"account": "a" * 129, "password": "private-password"},
        {"account": "alice", "password": "p" * 257},
    ],
)
def test_bind_request_rejects_empty_or_oversized_credentials(payload):
    with pytest.raises(ValidationError):
        McpCredentialBind.model_validate(payload)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, instance):
        self.instance = instance
        self.credentials = {}
        self.logs = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        params = list(statement.compile().params.values())
        if entity is McpInstance:
            requested_code = next((v for v in params if v == self.instance.code), None)
            return _ScalarResult(self.instance if requested_code else None)
        if entity is PlatformCredential:
            uid = next((v for v in params if isinstance(v, str) and v.startswith("user-")), None)
            platform = next((v for v in params if v == "internal_datahub"), None)
            return _ScalarResult(self.credentials.get((uid, platform)))
        raise AssertionError(f"unexpected query entity: {entity}")

    def add(self, row):
        if isinstance(row, PlatformCredential):
            self.credentials[(row.user_uid, row.platform)] = row
        elif isinstance(row, CredentialAccessLog):
            self.logs.append(row)
        else:
            raise AssertionError(f"unexpected row: {type(row)}")

    async def delete(self, row):
        self.credentials.pop((row.user_uid, row.platform), None)

    async def commit(self):
        self.commits += 1


class _SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


@pytest.fixture
def credential_service(monkeypatch):
    from services import mcp_credentials

    instance = McpInstance(
        id=7,
        code="datahub-main",
        name="中台",
        adapter_key="internal_datahub",
        auth_config_encrypted=None,
        config_json='{"timeout_seconds":30}',
        status=1,
    )
    session = _FakeSession(instance)
    adapter = SimpleNamespace(
        permission_mode="upstream_account",
        credential_platform="internal_datahub",
        health=AsyncMock(return_value={"ok": True, "latency_ms": 12}),
    )

    async def session_factory():
        return _SessionFactory(session)

    monkeypatch.setattr(mcp_credentials, "init_async_session", session_factory)
    monkeypatch.setattr(mcp_credentials, "get_adapter", lambda _key: adapter)
    monkeypatch.setattr(
        mcp_credentials,
        "encrypt_secret",
        lambda value, _purpose: f"encrypted::{value}",
    )
    monkeypatch.setattr(
        mcp_credentials,
        "decrypt_secret",
        lambda value, _purpose: value.removeprefix("encrypted::"),
    )
    return mcp_credentials, session, adapter, instance


async def test_bind_verifies_before_storing_encrypted_password_and_owner_audit(
    credential_service,
):
    service, session, adapter, _instance = credential_service

    result = await service.bind_and_verify(
        "user-a", "datahub-main", "alice", "private-password", "req-1", "10.0.0.1"
    )

    row = session.credentials[("user-a", "internal_datahub")]
    assert row.account == "alice"
    assert row.secret_encrypted == "encrypted::private-password"
    assert row.secret_encrypted != "private-password"
    assert row.status == 1
    assert row.last_verified_at is not None
    assert result == {
        "configured": True,
        "status": "verified",
        "last_verified_at": row.last_verified_at.isoformat(),
    }
    adapter.health.assert_awaited_once_with(
        {"account": "alice", "password": "private-password"},
        {"timeout_seconds": 30},
    )
    [log] = session.logs
    assert (log.operator_uid, log.target_user_uid, log.action) == (
        "user-a", "user-a", "create",
    )
    assert log.request_id == "req-1"
    assert log.client_ip == "10.0.0.1"


async def test_failed_bind_does_not_store_credentials(credential_service):
    service, session, adapter, _instance = credential_service
    adapter.health.side_effect = McpUpstreamError(
        "datahub_auth_failed", "中台账号或密码验证失败"
    )

    with pytest.raises(McpUpstreamError):
        await service.bind_and_verify(
            "user-a", "datahub-main", "alice", "wrong", "req-2", None
        )

    assert session.credentials == {}
    assert session.logs == []


async def test_stale_failed_test_does_not_invalidate_a_newer_credential(
    credential_service,
):
    service, session, adapter, _instance = credential_service
    original = PlatformCredential(
        id=9,
        user_uid="user-a",
        platform="internal_datahub",
        account="old-account",
        secret_encrypted="encrypted::old-password",
        status=1,
        last_verified_at=service.now(),
    )
    replacement = PlatformCredential(
        id=9,
        user_uid="user-a",
        platform="internal_datahub",
        account="new-account",
        secret_encrypted="encrypted::new-password",
        status=1,
        last_verified_at=service.now(),
    )
    session.credentials[("user-a", "internal_datahub")] = original

    async def fail_after_replacement(*_args, **_kwargs):
        session.credentials[("user-a", "internal_datahub")] = replacement
        raise McpUpstreamError(
            "datahub_auth_failed", "中台账号或密码验证失败"
        )

    adapter.health.side_effect = fail_after_replacement

    with pytest.raises(McpUpstreamError):
        await service.verify_saved(
            "user-a", "datahub-main", "req-race", "10.0.0.3"
        )

    assert replacement.status == 1
    assert replacement.account == "new-account"
    assert replacement.secret_encrypted == "encrypted::new-password"


async def test_stale_successful_test_does_not_verify_a_newer_invalid_credential(
    credential_service,
):
    service, session, adapter, _instance = credential_service
    original = PlatformCredential(
        id=10,
        user_uid="user-a",
        platform="internal_datahub",
        account="old-account",
        secret_encrypted="encrypted::old-password",
        status=1,
        last_verified_at=service.now(),
    )
    replacement = PlatformCredential(
        id=10,
        user_uid="user-a",
        platform="internal_datahub",
        account="new-account",
        secret_encrypted="encrypted::new-password",
        status=0,
        last_verified_at=None,
    )
    session.credentials[("user-a", "internal_datahub")] = original

    async def succeed_after_replacement(*_args, **_kwargs):
        session.credentials[("user-a", "internal_datahub")] = replacement
        return {"ok": True, "latency_ms": 12}

    adapter.health.side_effect = succeed_after_replacement

    result = await service.verify_saved(
        "user-a", "datahub-main", "req-success-race", "10.0.0.4"
    )

    assert result == {
        "configured": True,
        "status": "invalid",
        "last_verified_at": None,
    }
    assert replacement.status == 0
    assert replacement.last_verified_at is None


async def test_load_for_execute_uses_only_callers_own_verified_credential(
    credential_service,
):
    service, session, _adapter, instance = credential_service
    session.credentials[("user-a", "internal_datahub")] = PlatformCredential(
        user_uid="user-a",
        platform="internal_datahub",
        account="alice",
        secret_encrypted="encrypted::private-password",
        status=1,
        last_verified_at=service.now(),
    )

    auth = await service.load_for_execute("user-a", instance, "req-3")

    assert auth == {"account": "alice", "password": "private-password"}
    [log] = session.logs
    assert (log.operator_uid, log.target_user_uid, log.action) == (
        "user-a", "user-a", "decrypt",
    )

    with pytest.raises(PermissionError_, match="绑定并测试"):
        await service.load_for_execute("user-b", instance, "req-4")
    assert len(session.logs) == 1


async def test_delete_removes_only_owner_credential_and_audits(credential_service):
    service, session, _adapter, _instance = credential_service
    session.credentials[("user-a", "internal_datahub")] = PlatformCredential(
        user_uid="user-a",
        platform="internal_datahub",
        account="alice",
        secret_encrypted="encrypted::private-password",
        status=1,
        last_verified_at=service.now(),
    )
    session.credentials[("user-b", "internal_datahub")] = PlatformCredential(
        user_uid="user-b",
        platform="internal_datahub",
        account="bob",
        secret_encrypted="encrypted::other-password",
        status=1,
        last_verified_at=service.now(),
    )

    await service.delete_credential(
        "user-a", "datahub-main", "req-5", "10.0.0.2"
    )

    assert ("user-a", "internal_datahub") not in session.credentials
    assert ("user-b", "internal_datahub") in session.credentials
    [log] = session.logs
    assert (log.operator_uid, log.target_user_uid, log.action) == (
        "user-a", "user-a", "delete",
    )


def _request(method: str = "PUT") -> Request:
    request = Request({
        "type": "http",
        "method": method,
        "path": "/v1/mcp/instances/datahub-main/credential",
        "headers": [],
        "client": ("10.0.0.8", 1234),
        "query_string": b"",
        "server": ("test", 80),
        "scheme": "http",
    })
    request.state.request_id = "req-route"
    return request


@pytest.fixture
def caller():
    return Caller(
        uid="user-a",
        username="alice",
        role="employee",
        dept_id=1,
        dept_code="ops",
    )


async def test_bind_route_derives_owner_from_caller_and_never_returns_secrets(
    monkeypatch, caller
):
    from router.v1 import mcp_credentials as route

    bind = AsyncMock(return_value={
        "configured": True,
        "status": "verified",
        "last_verified_at": "2026-08-17T12:00:00",
    })
    monkeypatch.setattr(route.service, "bind_and_verify", bind)

    response = await route.credential_bind(
        "datahub-main",
        McpCredentialBind(account="alice", password="private-password"),
        _request(),
        caller,
    )

    bind.assert_awaited_once_with(
        "user-a",
        "datahub-main",
        "alice",
        "private-password",
        "req-route",
        "10.0.0.8",
    )
    assert response.data == {
        "configured": True,
        "status": "verified",
        "last_verified_at": "2026-08-17T12:00:00",
    }
    assert "private-password" not in response.model_dump_json()


def test_credential_router_exposes_one_shared_owner_scoped_api():
    from router.v1.mcp_credentials import router

    routes = {(route.path, tuple(sorted(route.methods))) for route in router.routes}
    assert routes == {
        ("/mcp/instances/{instance_code}/credential", ("GET",)),
        ("/mcp/instances/{instance_code}/credential", ("PUT",)),
        ("/mcp/instances/{instance_code}/credential/test", ("POST",)),
        ("/mcp/instances/{instance_code}/credential", ("DELETE",)),
    }
