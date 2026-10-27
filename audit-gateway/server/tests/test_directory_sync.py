"""用真实临时数据库验证差分、批量事务、权限冲突和密码不重放。"""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from model.database import (Department, DirectoryOperation, DirectorySnapshot,
                            ExternalIdentity, User, now)
from model.request.directory import DirectoryAction, DirectoryApply
from model.response import UserInfo
from services import directory_sync, dingtalk_directory, dingtalk_identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from utils.errors import ValidationError_
from utils.crypt import check_password_async

OPERATOR = UserInfo(id=99, uid="admin", permission="president")
MEMBER = {"user_id": "ding-1", "subject_id": "union-1", "name": "张三", "dept_ids": ["2"]}
ROSTER = {"members": [MEMBER], "departments": [{"id": "2", "name": "研发"}],
          "scope": {"departments": ["1"]}, "errors": [], "complete": True}


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "directory.db"))
    async with engine.begin() as conn:
        for table in (User.__table__, Department.__table__, ExternalIdentity.__table__,
                      DirectorySnapshot.__table__, DirectoryOperation.__table__):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(directory_sync, "init_async_session", AsyncMock(return_value=factory))
    monkeypatch.setattr(directory_sync, "get_settings", lambda: SimpleNamespace(dingtalk_corp_id="corp"))
    async with factory() as session:
        session.add(Department(id=2, code="dev", name="研发", dingtalk_dept_id="2", status=1))
        session.add(User(uid="local", username="花名", realname="张三", nickname="花名", password="hash",
                         permission="employee", status=1, account_kind="internal"))
        session.add(DirectorySnapshot(id="snap", provider="dingtalk", tenant_id="corp", operator_uid="admin",
                                      created_at=now(), completed_at=now(), complete=True,
                                      payload=json.dumps(ROSTER)))
        await session.commit()
    yield factory
    await engine.dispose()


async def run(action, operator=OPERATOR):
    return await directory_sync.apply(DirectoryApply(snapshot_id="snap", actions=[action]), operator)


@pytest.mark.asyncio
async def test_names_only_suggest_candidates_and_reviewed_binding_is_idempotent(database):
    async with database() as session:
        snap = await session.get(DirectorySnapshot, "snap")
        view = await directory_sync._preview_payload(session, snap, OPERATOR)
    row = view["rows"][0]
    assert row["category"] == "candidate" and row["can_bind"]
    assert row["candidates"][0]["uid"] == "local"
    assert not row["can_create"]
    action = DirectoryAction(key="member:ding-1", action="bind", uid="local")
    first = await run(action)
    second = await run(action)
    assert first["results"][0]["status"] == "success"
    assert second["results"][0]["status"] == "already_applied"
    async with database() as session:
        identities = list((await session.execute(select(ExternalIdentity))).scalars())
        assert len(identities) == 1
        assert (identities[0].subject_id, identities[0].user_uid) == ("union-1", "local")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,role,status", [("external", "employee", 1), ("internal", "president", 1), ("internal", "employee", 0)])
async def test_binding_rechecks_account_type_status_and_hierarchy(database, kind, role, status):
    async with database() as session:
        user = (await session.execute(select(User).where(User.uid == "local"))).scalar_one()
        user.account_kind, user.permission, user.status = kind, role, status
        await session.commit()
    result = await run(DirectoryAction(key="member:ding-1", action="bind", uid="local"))
    assert result["results"][0]["status"] == "failed"
    async with database() as session:
        assert list((await session.execute(select(ExternalIdentity))).scalars()) == []


@pytest.mark.asyncio
async def test_create_hashes_password_and_never_replays_credential(database):
    async with database() as session:
        snap = await session.get(DirectorySnapshot, "snap")
        payload = json.loads(snap.payload)
        payload["members"][0]["name"] = "新员工"
        snap.payload = json.dumps(payload)
        await session.commit()
    action = DirectoryAction(key="member:ding-1", action="create", username="新花名", dept_id=2)
    result = await run(action)
    password = result["credentials"][0]["initial_password"]
    async with database() as session:
        user = (await session.execute(select(User).where(User.username == "新花名"))).scalar_one()
        assert user.must_change_password and user.permission == "employee" and user.dept_id == 2
        assert user.password != password and await check_password_async(password, user.password)
        snap = await session.get(DirectorySnapshot, "snap")
        assert password not in snap.payload
    retry = await run(action)
    assert retry["credentials"] == []
    assert retry["results"][0]["status"] == "already_applied"


@pytest.mark.asyncio
async def test_identity_conflict_rolls_back_created_account(database):
    async with database() as session:
        snap = await session.get(DirectorySnapshot, "snap")
        payload = json.loads(snap.payload)
        payload["members"][0]["name"] = "新员工"
        snap.payload = json.dumps(payload)
        session.add(ExternalIdentity(provider="dingtalk", tenant_id="corp", subject_id="another-union",
                                     provider_user_id="ding-1", user_uid="other"))
        await session.commit()
    result = await run(DirectoryAction(key="member:ding-1", action="create", username="新花名"))
    assert result["results"][0]["status"] == "failed" and not result["credentials"]
    async with database() as session:
        assert (await session.execute(select(User).where(User.username == "新花名"))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_batch_partial_failure_can_retry_without_duplicate_binding(database):
    body = DirectoryApply(snapshot_id="snap", actions=[
        DirectoryAction(key="member:ding-1", action="bind", uid="local"),
        DirectoryAction(key="member:forged", action="bind", uid="local"),
    ])
    result = await directory_sync.apply(body, OPERATOR)
    assert [r["status"] for r in result["results"]] == ["success", "failed"]
    retry = await directory_sync.apply(body, OPERATOR)
    assert [r["status"] for r in retry["results"]] == ["already_applied", "failed"]


@pytest.mark.asyncio
async def test_stale_or_foreign_preview_is_rejected(database):
    with pytest.raises(ValidationError_):
        await run(DirectoryAction(key="member:ding-1", action="bind", uid="local"),
                  UserInfo(id=98, uid="other-admin", permission="president"))
    async with database() as session:
        snap = await session.get(DirectorySnapshot, "snap")
        snap.created_at = now() - timedelta(hours=1)
        await session.commit()
    with pytest.raises(ValidationError_):
        await run(DirectoryAction(key="member:ding-1", action="bind", uid="local"))


@pytest.mark.asyncio
async def test_disable_requires_two_complete_snapshots_and_revokes_same_credentials(database, monkeypatch):
    tokens = SimpleNamespace(revoke_user_tokens=AsyncMock())
    keys = AsyncMock()
    monkeypatch.setattr(directory_sync, "get_token_manager", lambda: tokens)
    monkeypatch.setattr(directory_sync, "revoke_user_api_keys", keys)
    async with database() as session:
        session.add(ExternalIdentity(provider="dingtalk", tenant_id="corp", subject_id="union-1", user_uid="local",
                                     provider_user_id="ding-1", created_at=now() - timedelta(days=3)))
        snap = await session.get(DirectorySnapshot, "snap")
        roster = {**ROSTER, "members": []}
        snap.payload = json.dumps(roster)
        await session.commit()
    action = DirectoryAction(key="account:local", action="disable")
    assert (await run(action))["results"][0]["status"] == "failed"
    async with database() as session:
        session.add(DirectorySnapshot(id="previous", provider="dingtalk", tenant_id="corp", operator_uid="admin",
                                      created_at=now() - timedelta(days=1), completed_at=now() - timedelta(days=1),
                                      complete=True, payload=json.dumps(roster)))
        await session.commit()
    assert (await run(action))["results"][0]["status"] == "success"
    tokens.revoke_user_tokens.assert_awaited_once_with("local", reason="account_disabled")
    keys.assert_awaited_once_with("local", reason="account_disabled", by=OPERATOR.uid)


def test_external_missing_and_partial_snapshots_never_offer_disable():
    user = User(uid="external", username="外部", permission="employee", account_kind="external", status=1)
    identity = ExternalIdentity(subject_id="u", provider_user_id="member", user_uid="external", created_at=now() - timedelta(days=3))
    for complete in (True, False):
        roster = {"members": [], "complete": complete}
        rows = directory_sync.make_diff(roster, [user], [identity], [], OPERATOR,
                                         {"members": [], "complete": True, "started_at": now()})
        assert rows[0]["category"] == "external" and not rows[0]["can_disable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "partial", "loop", "denied"])
async def test_real_http_adapter_checks_scope_and_all_pages(monkeypatch, mode):
    seen = []
    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        seen.append((path, body))
        if path.endswith("/accessToken"):
            return httpx.Response(200, json={"accessToken": "server-only-token", "expireIn": 7200})
        if path == "/auth/scopes":
            return httpx.Response(200, json={"errcode": 0, "auth_org_scopes": {"authed_dept": [2] if mode == "partial" else [1], "authed_user": []}})
        if "department" in path:
            return httpx.Response(200, json={"errcode": 60011} if mode == "denied" else {"errcode": 0, "result": []})
        cursor = body["cursor"]
        result = {"list": [{"userid": "member-" + str(cursor), "unionid": "union-" + str(cursor), "name": "名字", "dept_id_list": [1]}],
                  "has_more": cursor == 0, "next_cursor": 0 if mode == "loop" else 100}
        return httpx.Response(200, json={"errcode": 0, "result": result})
    original = httpx.AsyncClient
    monkeypatch.setattr(dingtalk_directory.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    dingtalk_identity._app_token_cache.clear()
    cfg = SimpleNamespace(dingtalk_client_id="app", dingtalk_client_secret="secret", dingtalk_corp_id="corp")
    roster = await dingtalk_directory.fetch_roster(cfg)
    assert roster.complete == (mode == "full")
    assert len(roster.members) == (2 if mode in {"full", "partial"} else 1 if mode == "loop" else 0)
    assert "server-only-token" not in str(roster)
    if mode != "full": assert roster.errors

@pytest.mark.asyncio
async def test_initial_password_token_cannot_create_keys_or_call_v1(monkeypatch):
    from utils import dependencies
    from utils.crypt import JWTAuth
    user = SimpleNamespace(id=1, uid="uid", username="员工", nickname="员工", realname="名字",
                           permission="employee", dept_id=None, status=1, must_change_password=True)
    monkeypatch.setattr(dependencies, "get_user_info", AsyncMock(return_value=user))
    token = JWTAuth().create_access_token(user, password_change_required=True)
    assert (await dependencies.check_token(token)).code == 10012
    allowed = await dependencies.check_token(token, allow_password_change=True)
    assert allowed.must_change_password and allowed.uid == "uid"
    # 钉钉已验证身份的会话不依赖初始密码；仅密码登录生成受限会话。
    assert isinstance(await dependencies.check_token(JWTAuth().create_access_token(user)), UserInfo)
    user.must_change_password = False
    assert isinstance(await dependencies.check_token(token), UserInfo)


@pytest.mark.asyncio
async def test_cached_or_legacy_admin_cannot_bypass_disabled_status(monkeypatch):
    from utils import dependencies
    from utils.crypt import JWTAuth
    from starlette.requests import Request
    user = SimpleNamespace(id=1, uid="uid", username="员工", nickname="员工", realname="名字",
                           permission="admin", dept_id=None, status=0, must_change_password=False)
    monkeypatch.setattr(dependencies, "get_user_info", AsyncMock(return_value=user))
    monkeypatch.setattr(dependencies.token_manager, "is_token_revoked", AsyncMock(return_value=False))
    token = JWTAuth().create_access_token(user)
    request = Request({"type": "http", "method": "GET", "path": "/admin/apikey/create", "headers": []})
    info = await dependencies.get_current_user(token, request)
    assert info.code == 10007

@pytest.mark.asyncio
async def test_export_audit_contains_only_created_user_references(database):
    from model.request.directory import DirectoryExport
    async with database() as session:
        snap = await session.get(DirectorySnapshot, "snap")
        payload = json.loads(snap.payload)
        payload["members"][0]["name"] = "新员工"
        snap.payload = json.dumps(payload)
        await session.commit()
    result = await run(DirectoryAction(key="member:ding-1", action="create", username="新花名"))
    uid = result["credentials"][0]["uid"]
    export = DirectoryExport(snapshot_id="snap", user_uids=[uid])
    await directory_sync.audit_export(export, OPERATOR)
    await directory_sync.audit_export(export, OPERATOR)
    async with database() as session:
        events = list((await session.execute(select(DirectoryOperation).where(DirectoryOperation.action == "export"))).scalars())
        assert len(events) == 1 and events[0].user_uid == uid
    with pytest.raises(ValidationError_):
        await directory_sync.audit_export(DirectoryExport(snapshot_id="snap", user_uids=["not-created"]), OPERATOR)


def test_sync_api_requires_user_edit_and_sets_no_store(monkeypatch):
    from router.admin import directory
    from utils import dependencies
    from utils.middleware import register_exception_handlers
    app = FastAPI()
    app.include_router(directory.router, prefix="/admin")
    register_exception_handlers(app)
    snapshot = AsyncMock(return_value={"snapshot_id": "safe"})
    monkeypatch.setattr(directory.directory_sync, "preview", snapshot)
    app.dependency_overrides[dependencies.get_current_user] = lambda: UserInfo(id=1, uid="employee", permission="employee")
    with TestClient(app) as client:
        denied = client.post("/admin/account/directory/preview")
        assert denied.json()["code"] == 10008
        snapshot.assert_not_awaited()
        app.dependency_overrides[dependencies.get_current_user] = lambda: OPERATOR
        allowed = client.post("/admin/account/directory/preview")
        assert allowed.json()["data"]["snapshot_id"] == "safe"
        assert allowed.headers["cache-control"] == "no-store"
