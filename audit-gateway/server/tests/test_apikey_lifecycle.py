"""设备 API Key 的闲置失效与吊销台账（2026-10-19）

最要紧的一条是**不能踢掉正在用的客户端**（2026-09 夜间掉线事故之后的硬约束）。
客户端每次刷新 refresh token 都会紧接着用设备 Key 调一次 /v1/models
（apps/desktop/electron/product-auth.ts · ProductSessionCoordinator.validate），
所以只要闲置阈值高于 refresh 有效期，一台还能续期的客户端的 Key 就不可能闲置失效。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import ApiKey, Base, User, now
from model.response import UserInfo
from services import apikey_lifecycle as lc
from utils.apikey import generate_api_key
from utils.dependencies import require_login
from utils.errors import ApiKeyAuthError
from utils.middleware import register_exception_handlers

REFRESH_MINUTES = 7 * 24 * 60
T0 = datetime(2026, 10, 1, 9, 0, 0)


def _key(last_used=None, created=T0, status=1, expires_at=None, scope="private"):
    return SimpleNamespace(status=status, expires_at=expires_at, last_used_at=last_used,
                           created_at=created, scope=scope)


# ---------------- 纯规则 ----------------


def test_阈值不低于refresh有效期加一天():
    assert lc.effective_idle_days(30, REFRESH_MINUTES) == 30
    assert lc.effective_idle_days(3, REFRESH_MINUTES) == 8
    assert lc.effective_idle_days(7, REFRESH_MINUTES) == 8
    assert lc.effective_idle_days(0, REFRESH_MINUTES) == 0


@pytest.mark.parametrize("configured", [1, 3, 7, 8, 30])
def test_还能续期的客户端的Key永远不会闲置失效(configured):
    """契约：客户端最后一次续期 = Key 最后一次使用。在 refresh token 过期前的
    任何时刻，这把 Key 都必须是有效的 —— 不管阈值配成多少。"""
    idle_days = lc.effective_idle_days(configured, REFRESH_MINUTES)
    last_refresh = T0
    key = _key(last_used=last_refresh)
    refresh_expires = last_refresh + timedelta(minutes=REFRESH_MINUTES)
    moment = last_refresh
    while moment < refresh_expires:
        assert lc.key_state(key, moment, idle_days) == lc.STATE_ACTIVE, moment
        moment += timedelta(hours=1)
    # 而 refresh 也过期之后再过一天，Key 就该清掉了（只要阈值配得不比这更宽）
    if configured <= 8:
        assert lc.key_state(key, refresh_expires + timedelta(days=1, hours=1), idle_days) == lc.STATE_IDLE


def test_从没用过的Key按签发时间算():
    key = _key(last_used=None, created=T0)
    assert lc.is_idle(key, T0 + timedelta(days=29), 30) is False
    assert lc.is_idle(key, T0 + timedelta(days=31), 30) is True


def test_群聊令牌不参与闲置判定():
    assert lc.is_idle(_key(scope="group"), T0 + timedelta(days=90), 30) is False
    assert lc.idle_deadline(_key(scope="group"), 30) is None


def test_状态优先级与认证拒绝顺序一致():
    old = T0 - timedelta(days=60)
    assert lc.key_state(_key(last_used=old, status=0), T0, 30) == lc.STATE_REVOKED
    assert lc.key_state(_key(last_used=old, expires_at=T0 - timedelta(days=1)), T0, 30) == lc.STATE_EXPIRED
    assert lc.key_state(_key(last_used=old), T0, 30) == lc.STATE_IDLE
    assert lc.key_state(_key(last_used=old), T0, 0) == lc.STATE_ACTIVE


# ---------------- 真实库：认证、吊销台账、后台总览 ----------------


@pytest.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'keys.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[ApiKey.__table__, User.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def factory():
        return maker

    import db as db_pkg
    import db.selects.apikey as sel
    import db.updates.apikey as upd
    import db.selects.user as usr
    import db.selects.department as dep
    for mod in (db_pkg, sel, upd, usr, dep):
        monkeypatch.setattr(mod, "init_async_session", factory)
    monkeypatch.setattr(lc, "configured_idle_days", lambda: 30)
    import utils.gateway_auth as ga
    monkeypatch.setattr(ga, "configured_idle_days", lambda: 30)
    import router.admin.apikey as admin_apikey
    monkeypatch.setattr(admin_apikey, "configured_idle_days", lambda: 30)

    async with maker() as session:
        for uid, role in (("u-boss", "president"), ("u-dev", "developer"), ("u-emp", "employee")):
            session.add(User(uid=uid, username=uid, realname=uid, password="x",
                             permission=role, status=1))
        await session.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


async def _issue(maker, uid, days_idle, name="Mac", scope="private", status=1):
    plain, prefix, hashed = generate_api_key()
    async with maker() as session:
        key = ApiKey(user_uid=uid, name=name, key_prefix=prefix, key_hash=hashed, status=status,
                     scope=scope, created_at=now() - timedelta(days=days_idle + 1),
                     last_used_at=now() - timedelta(days=days_idle))
        session.add(key)
        await session.commit()
        return plain, key.id


async def test_闲置超过阈值的Key认证时被拒(db):
    from utils.gateway_auth import _caller_from_api_key

    fresh, _ = await _issue(db, "u-emp", days_idle=6)
    stale, _ = await _issue(db, "u-emp", days_idle=31)

    caller = await _caller_from_api_key(fresh)
    assert caller.uid == "u-emp"
    with pytest.raises(ApiKeyAuthError) as caught:
        await _caller_from_api_key(stale)
    assert str(caught.value.message) == lc.IDLE_MESSAGE
    assert caught.value.http_status == 401


async def test_吊销记下原因且不覆盖第一次吊销(db):
    from sqlalchemy import select

    from db.updates.apikey import revoke_api_key, revoke_user_api_keys

    _, first = await _issue(db, "u-emp", days_idle=1)
    _, second = await _issue(db, "u-emp", days_idle=1)
    await revoke_api_key(first, reason="client_logout")
    await revoke_user_api_keys("u-emp", reason="account_disabled", by="u-boss")

    async with db() as session:
        rows = {k.id: k for k in (await session.execute(select(ApiKey))).scalars()}
    assert (rows[first].revoke_reason, rows[first].revoked_by) == ("client_logout", None)
    assert (rows[second].revoke_reason, rows[second].revoked_by) == ("account_disabled", "u-boss")
    assert rows[first].revoked_at is not None


@pytest.fixture
def client(db):
    from router.admin import router as admin_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_router)
    app.dependency_overrides[require_login] = lambda: UserInfo(
        id=2, uid="u-dev", username="dev", permission="developer", dept_id=None,
        dept_code=None, permissions=[])
    return TestClient(app)


async def test_后台总览按状态筛选并显示持有人(db, client):
    await _issue(db, "u-emp", days_idle=2, name="在用")
    await _issue(db, "u-emp", days_idle=40, name="闲置")
    await _issue(db, "u-emp", days_idle=2, name="已退出", status=0)

    data = client.get("/admin/apikey/overview").json()["data"]
    assert data["total"] == 3 and data["idle_days"] == 30
    # 最久没用的排最前
    assert [i["name"] for i in data["items"]][0] == "闲置"
    states = {i["name"]: i["state"] for i in data["items"]}
    assert states == {"在用": "active", "闲置": "idle", "已退出": "revoked"}
    assert data["items"][0]["owner"]["name"] == "u-emp"
    assert "key_hash" not in data["items"][0]

    idle_only = client.get("/admin/apikey/overview", params={"state": "idle"}).json()["data"]
    assert [i["name"] for i in idle_only["items"]] == ["闲置"]
    assert client.get("/admin/apikey/overview", params={"state": "bogus"}).json()["code"] != 0


async def test_批量吊销跳过无权操作的Key(db, client):
    _, emp_key = await _issue(db, "u-emp", days_idle=1)
    _, boss_key = await _issue(db, "u-boss", days_idle=1)
    body = client.post("/admin/apikey/batch-revoke", json={"ids": [emp_key, boss_key]}).json()
    assert body["data"] == {"revoked": 1, "denied_ids": [boss_key]}


async def test_清理闲置先预览再执行(db, client):
    await _issue(db, "u-emp", days_idle=40)
    await _issue(db, "u-emp", days_idle=3)
    preview = client.post("/admin/apikey/revoke-idle", json={"dry_run": True}).json()["data"]
    assert preview["would_revoke"] == 1
    done = client.post("/admin/apikey/revoke-idle", json={"dry_run": False}).json()["data"]
    assert done["revoked"] == 1
    states = [i["state"] for i in client.get("/admin/apikey/overview").json()["data"]["items"]]
    assert sorted(states) == ["active", "revoked"]
    reasons = [i["revoke_reason"] for i in client.get(
        "/admin/apikey/overview", params={"state": "revoked"}).json()["data"]["items"]]
    assert reasons == ["idle_30d"]
    revoked_by = [i["revoked_by"] for i in client.get(
        "/admin/apikey/overview", params={"state": "revoked"}).json()["data"]["items"]]
    assert revoked_by == ["u-dev"]


async def test_普通员工不能看全公司Key(db, client):
    client.app.dependency_overrides[require_login] = lambda: UserInfo(
        id=3, uid="u-emp", username="emp", permission="employee", dept_id=None,
        dept_code=None, permissions=[])
    assert client.get("/admin/apikey/overview").json()["code"] == 10008


async def test_巡检吊销全部闲置个人Key并记原因(db):
    from sqlalchemy import select

    _, stale = await _issue(db, "u-emp", days_idle=31)
    _, fresh = await _issue(db, "u-emp", days_idle=29)
    _, group = await _issue(db, "u-emp", days_idle=90, scope="group")
    _, already = await _issue(db, "u-emp", days_idle=90, status=0)

    assert await lc.sweep_idle_keys() == 1
    assert await lc.sweep_idle_keys() == 0  # 幂等

    async with db() as session:
        rows = {k.id: k for k in (await session.execute(select(ApiKey))).scalars()}
    assert (rows[stale].status, rows[stale].revoke_reason, rows[stale].revoked_by) == (0, "idle_30d", "system")
    assert rows[fresh].status == 1
    assert rows[group].status == 1
    assert rows[already].revoke_reason is None  # 不覆盖更早的吊销记录


async def test_认证碰到闲置Key当场吊销(db):
    import asyncio

    from sqlalchemy import select

    from utils.gateway_auth import _caller_from_api_key

    stale, key_id = await _issue(db, "u-emp", days_idle=40)
    with pytest.raises(ApiKeyAuthError):
        await _caller_from_api_key(stale)
    for _ in range(50):
        await asyncio.sleep(0.01)
        async with db() as session:
            row = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one()
        if row.status == 0:
            break
    assert (row.status, row.revoke_reason) == (0, "idle_30d")


def test_巡检关闭时不启动(monkeypatch):
    monkeypatch.setattr(lc, "_sweep_task", None)
    lc.start_idle_sweeper(0)
    assert lc._sweep_task is None
