"""版本化系统提示词策略的权限、规范化与发布生命周期。"""

from __future__ import annotations

import asyncio
import re
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import (
    ROLE_AUDITOR,
    ROLE_DEVELOPER,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    ROLE_PRESIDENT,
    PromptPolicyAuditLog,
    PromptPolicyDraft,
    PromptPolicyVersion,
)
from model.org_defaults import AUDIT_DEPT_CODE
from model.response import UserInfo
from services import prompt_policy as policy
from services.permissions import has_permission
from utils.dependencies import require_login
from utils.errors import AppError, ConflictError, ValidationError_
from utils.gateway_auth import Caller, require_caller
from utils.middleware import register_exception_handlers


OTHER_DEPT = "rnd"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrations"
    / "2026-09-17-prompt-policy.sql"
)


def _actor(
    role: str = ROLE_DEVELOPER,
    *,
    uid: str = "u-dev",
    username: str = "dev",
    dept_code: str = OTHER_DEPT,
):
    return SimpleNamespace(
        uid=uid,
        username=username,
        permission=role,
        dept_code=dept_code,
    )


def _user(role: str, *, dept_code: str = OTHER_DEPT) -> UserInfo:
    return UserInfo(
        id=1,
        uid=f"u-{role}",
        username=role,
        permission=role,
        dept_id=1,
        dept_code=dept_code,
    )


# ── 服务端权限边界 ───────────────────────────────────────────────────────


def test_prompt_policy_permissions_match_the_confirmed_role_and_department_rules():
    # 总裁可以查看和发布，但用户明确没有把草稿编辑权自动授给总裁。
    assert has_permission(ROLE_PRESIDENT, "prompt_policy:view", OTHER_DEPT)
    assert not has_permission(ROLE_PRESIDENT, "prompt_policy:edit", OTHER_DEPT)
    assert has_permission(ROLE_PRESIDENT, "prompt_policy:publish", OTHER_DEPT)

    # 开发者负责日常维护，不受所在部门限制。
    for permission in (
        "prompt_policy:view",
        "prompt_policy:edit",
        "prompt_policy:publish",
    ):
        assert has_permission(ROLE_DEVELOPER, permission, OTHER_DEPT)
        assert has_permission("admin", permission, OTHER_DEPT)

    # 管理部门负责人能看、能改草稿，但不能发布。
    assert has_permission(ROLE_MANAGER, "prompt_policy:view", AUDIT_DEPT_CODE)
    assert has_permission(ROLE_MANAGER, "prompt_policy:edit", AUDIT_DEPT_CODE)
    assert not has_permission(ROLE_MANAGER, "prompt_policy:publish", AUDIT_DEPT_CODE)

    # 同一个 manager 离开管理部门后不能借角色越权。
    for permission in (
        "prompt_policy:view",
        "prompt_policy:edit",
        "prompt_policy:publish",
    ):
        assert not has_permission(ROLE_MANAGER, permission, OTHER_DEPT)

    # 管理部门审计员只读，其他部门审计员无权进入页面。
    assert has_permission(ROLE_AUDITOR, "prompt_policy:view", AUDIT_DEPT_CODE)
    assert not has_permission(ROLE_AUDITOR, "prompt_policy:edit", AUDIT_DEPT_CODE)
    assert not has_permission(ROLE_AUDITOR, "prompt_policy:publish", AUDIT_DEPT_CODE)
    assert not has_permission(ROLE_AUDITOR, "prompt_policy:view", OTHER_DEPT)

    assert not has_permission(ROLE_EMPLOYEE, "prompt_policy:view", AUDIT_DEPT_CODE)


# ── 规范化、hash 与大小边界 ──────────────────────────────────────────────


def test_policy_hash_is_stable_across_line_endings_and_outer_whitespace():
    unix = policy.normalize_package("规则一\n规则二", "人格一\n人格二")
    windows = policy.normalize_package("  规则一\r\n规则二\r\n", "\r人格一\r人格二  ")

    assert windows == unix
    assert policy.content_hash(*windows) == policy.content_hash(*unix)
    assert policy.content_hash(*unix) == (
        "sha256:c64a3f850359c37ddd08125e5b6f753efbba371bc3486e1139f566d098595e09"
    )


def test_policy_text_rejects_nul_invalid_unicode_and_oversized_utf8():
    with pytest.raises(ValidationError_, match="NUL"):
        policy.normalize_package("不能\x00出现", "人格")

    with pytest.raises(ValidationError_, match="UTF-8"):
        policy.normalize_package("\ud800", "人格")

    too_large = "规" * (policy.MAX_COMPANY_POLICY_CHARS + 1)
    with pytest.raises(ValidationError_, match="company_core_policy"):
        policy.normalize_package(too_large, "人格")

    # 两个字段分别不超字符数时，合计 UTF-8 仍有独立上限。
    with pytest.raises(ValidationError_, match="128000 UTF-8 字节"):
        policy.content_hash("😀" * policy.MAX_COMPANY_POLICY_CHARS, "人")


@pytest.mark.parametrize(
    "model",
    [PromptPolicyDraft, PromptPolicyVersion, PromptPolicyAuditLog],
)
def test_prompt_policy_migration_columns_match_the_orm(model):
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    table = model.__tablename__
    block = sql.split(f"CREATE TABLE IF NOT EXISTS `{table}`", 1)[1].split(
        "ENGINE=InnoDB", 1
    )[0]
    sql_columns = set(re.findall(r"^\s*`(\w+)`\s+[A-Z]", block, re.MULTILINE))
    orm_columns = {column.name for column in model.__table__.columns}

    assert sql_columns == orm_columns


def test_policy_body_columns_are_mediumtext_on_mysql():
    from sqlalchemy.dialects.mysql import dialect

    mysql = dialect()
    for model in (PromptPolicyDraft, PromptPolicyVersion):
        for column_name in ("company_core_policy", "default_persona"):
            compiled = model.__table__.c[column_name].type.compile(dialect=mysql)
            assert compiled.upper() == "MEDIUMTEXT"

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert len(re.findall(r"`company_core_policy`\s+MEDIUMTEXT", sql)) == 2
    assert len(re.findall(r"`default_persona`\s+MEDIUMTEXT", sql)) == 2


@pytest.fixture
async def policy_db(tmp_path, monkeypatch):
    database = tmp_path / "prompt-policy.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: PromptPolicyDraft.__table__.create(sync_conn)
        )
        await conn.run_sync(
            lambda sync_conn: PromptPolicyVersion.__table__.create(sync_conn)
        )
        await conn.run_sync(
            lambda sync_conn: PromptPolicyAuditLog.__table__.create(sync_conn)
        )

    async with maker() as session:
        session.add(PromptPolicyDraft(id=1, revision=0))
        await session.commit()

    async def fake_maker():
        return maker

    monkeypatch.setattr("db.init_async_session", fake_maker)
    yield maker
    await engine.dispose()


async def test_empty_database_has_an_explicit_unpublished_admin_state(policy_db):
    state = await policy.get_admin_state()

    assert state["published"] is None
    assert state["draft"]["revision"] == 0
    assert state["draft"]["company_core_policy"] == ""
    assert state["draft"]["default_persona"] == ""

    with pytest.raises(ValidationError_, match="还没有已发布策略"):
        await policy.rollback(
            target_version=1,
            expected_current_version=1,
            actor=_actor(ROLE_PRESIDENT),
        )


# ── 草稿 → 发布 → 回滚，且全部写操作带并发版本检查 ───────────────────────


async def test_draft_publish_and_rollback_are_append_only_and_audited(policy_db):
    developer = _actor()

    draft = await policy.save_draft(
        company_core_policy="公司规则 v1\r\n第二行",
        default_persona="默认人格 v1",
        expected_revision=0,
        actor=developer,
    )
    assert draft["revision"] == 1
    assert draft["company_core_policy"] == "公司规则 v1\n第二行"

    with pytest.raises(ConflictError, match="草稿已被其他人更新") as stale_draft:
        await policy.save_draft(
            company_core_policy="过期页面的覆盖",
            default_persona="过期人格",
            expected_revision=0,
            actor=developer,
        )
    assert stale_draft.value.http_status == 409
    assert stale_draft.value.data == {"current_revision": 1}

    first = await policy.publish_draft(expected_revision=1, actor=developer)
    assert first["version"] == 1
    assert first["source_version"] is None

    # publish 会推进 draft revision；同一个页面不能重复发布。
    with pytest.raises(ConflictError, match="草稿已被其他人更新"):
        await policy.publish_draft(expected_revision=1, actor=developer)

    draft = await policy.save_draft(
        company_core_policy="公司规则 v2",
        default_persona="默认人格 v2",
        expected_revision=2,
        actor=developer,
    )
    second = await policy.publish_draft(
        expected_revision=draft["revision"], actor=developer
    )
    assert second["version"] == 2
    assert second["source_version"] is None
    assert second["content_hash"] != first["content_hash"]

    rolled_back = await policy.rollback(
        target_version=1,
        expected_current_version=2,
        actor=_actor(ROLE_PRESIDENT, uid="u-president", username="president"),
    )
    assert rolled_back["version"] == 3
    assert rolled_back["source_version"] == 1
    assert rolled_back["content_hash"] == first["content_hash"]
    assert rolled_back["company_core_policy"] == first["company_core_policy"]

    state_after_rollback = await policy.get_admin_state()
    assert state_after_rollback["draft"]["updated_by_uid"] == "u-president"
    assert state_after_rollback["draft"]["updated_by"] == "president"

    with pytest.raises(ConflictError, match="当前发布版本已变化") as stale_publish:
        await policy.rollback(
            target_version=2,
            expected_current_version=2,
            actor=developer,
        )
    assert stale_publish.value.http_status == 409
    assert stale_publish.value.data == {"current_version": 3}

    with pytest.raises(ValidationError_, match="内容与当前发布版本相同"):
        await policy.rollback(
            target_version=1,
            expected_current_version=3,
            actor=developer,
        )

    async with policy_db() as session:
        versions = list(
            (
                await session.execute(
                    select(PromptPolicyVersion).order_by(PromptPolicyVersion.version)
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await session.execute(
                    select(PromptPolicyAuditLog).order_by(PromptPolicyAuditLog.id)
                )
            )
            .scalars()
            .all()
        )

    # 回滚是追加 v3，不会原地覆盖 v1/v2。
    assert [row.version for row in versions] == [1, 2, 3]
    assert versions[0].company_core_policy == "公司规则 v1\n第二行"
    assert versions[1].company_core_policy == "公司规则 v2"
    assert [row.action for row in events] == [
        "draft_created",
        "published",
        "draft_updated",
        "published",
        "rolled_back",
    ]
    assert all(row.actor_uid and row.content_hash for row in events)
    # 审计表只记 hash 与操作者，不复制正文。
    assert "company_core_policy" not in PromptPolicyAuditLog.__table__.columns
    assert "default_persona" not in PromptPolicyAuditLog.__table__.columns


async def test_publish_rejects_an_incomplete_or_unchanged_draft(policy_db):
    developer = _actor()

    with pytest.raises(ValidationError_, match="两个字段都不能为空"):
        await policy.publish_draft(expected_revision=0, actor=developer)

    await policy.save_draft(
        company_core_policy="规则",
        default_persona="人格",
        expected_revision=0,
        actor=developer,
    )
    await policy.publish_draft(expected_revision=1, actor=developer)

    # publish 后内容没变，不能用空版本污染不可变历史。
    with pytest.raises(ValidationError_, match="没有变化"):
        await policy.publish_draft(expected_revision=2, actor=developer)


async def test_concurrent_publish_and_rollback_have_exactly_one_winner(policy_db):
    developer = _actor()
    await policy.save_draft(
        company_core_policy="规则 v1",
        default_persona="人格 v1",
        expected_revision=0,
        actor=developer,
    )

    publish_results = await asyncio.gather(
        policy.publish_draft(expected_revision=1, actor=developer),
        policy.publish_draft(expected_revision=1, actor=developer),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in publish_results) == 1
    assert sum(
        isinstance(result, policy.PromptPolicyConflictError)
        for result in publish_results
    ) == 1

    await policy.save_draft(
        company_core_policy="规则 v2",
        default_persona="人格 v2",
        expected_revision=2,
        actor=developer,
    )
    second = await policy.publish_draft(expected_revision=3, actor=developer)
    assert second["version"] == 2

    rollback_results = await asyncio.gather(
        policy.rollback(target_version=1, expected_current_version=2, actor=developer),
        policy.rollback(target_version=1, expected_current_version=2, actor=developer),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in rollback_results) == 1
    assert sum(
        isinstance(result, policy.PromptPolicyConflictError)
        for result in rollback_results
    ) == 1

    history = await policy.get_history()
    assert [item["version"] for item in history["items"]] == [3, 2, 1]


async def test_write_failure_does_not_expose_policy_body_in_exception(monkeypatch):
    secret_body = "SENTINEL-COMPANY-POLICY-MUST-NOT-LEAK"

    class BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            # 模拟 SQLAlchemy/驱动把绑定参数拼进底层异常的最坏情况。
            raise SQLAlchemyError(f"failed params: {secret_body}")

    async def fake_maker():
        return BrokenSession

    monkeypatch.setattr("db.init_async_session", fake_maker)

    with pytest.raises(AppError) as raised:
        await policy.save_draft(
            company_core_policy=secret_body,
            default_persona="人格",
            expected_revision=0,
            actor=_actor(),
        )

    assert raised.value.http_status == 500
    assert str(raised.value) == "提示词策略存储失败"
    assert secret_body not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert secret_body not in "".join(traceback.format_exception(raised.value))


async def test_gateway_engine_hides_bound_parameters(monkeypatch):
    import db

    captured = {}
    sentinel_engine = object()

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return sentinel_engine

    monkeypatch.setattr(db, "create_async_engine", fake_create_async_engine)

    assert await db.create_db_engine() is sentinel_engine
    assert captured["hide_parameters"] is True


# ── 管理端路由：权限必须由服务端依赖项拦截 ───────────────────────────────


def _admin_app(user: UserInfo) -> FastAPI:
    from router.admin.prompt_policy import router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[require_login] = lambda: user
    return app


def _admin_app_with_live_auth() -> FastAPI:
    from router.admin.prompt_policy import router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/admin")
    return app


def test_admin_routes_enforce_view_edit_and_publish_separately(monkeypatch):
    rollback_calls = []
    draft = {
        "company_core_policy": "规则",
        "default_persona": "人格",
        "content_hash": "sha256:" + "a" * 64,
        "revision": 4,
        "base_version": 2,
        "updated_at": "2026-09-17T12:00:00",
        "updated_by": "developer",
        "updated_by_uid": "u-developer",
    }
    published = {
        "version": 2,
        "company_core_policy": "规则",
        "default_persona": "人格",
        "content_hash": "sha256:" + "a" * 64,
        "published_at": "2026-09-17T12:00:00",
        "published_by": "developer",
        "published_by_uid": "u-developer",
        "source_version": None,
    }

    async def fake_state():
        return {"published": published, "draft": draft}

    async def fake_save(**_kwargs):
        return draft

    async def fake_publish(**_kwargs):
        return published

    async def fake_rollback(**kwargs):
        rollback_calls.append(kwargs)
        return published

    monkeypatch.setattr("router.admin.prompt_policy.get_admin_state", fake_state)
    monkeypatch.setattr("router.admin.prompt_policy.save_draft", fake_save)
    monkeypatch.setattr("router.admin.prompt_policy.publish_draft", fake_publish)
    monkeypatch.setattr("router.admin.prompt_policy.rollback", fake_rollback)

    # 总裁可查看、可发布，但不能编辑草稿。
    president = TestClient(_admin_app(_user(ROLE_PRESIDENT)))
    assert president.get("/admin/prompt-policy").status_code == 200
    denied_edit = president.put(
        "/admin/prompt-policy/draft",
        json={
            "company_core_policy": "规则",
            "default_persona": "人格",
            "expected_revision": 4,
        },
    )
    assert denied_edit.json()["code"] == 10008
    assert (
        president.post(
            "/admin/prompt-policy/publish", json={"expected_revision": 4}
        ).json()["data"]["published"]["version"]
        == 2
    )
    assert (
        president.post(
            "/admin/prompt-policy/rollback/1",
            json={"expected_current_version": 2},
        ).json()["data"]["published"]["version"]
        == 2
    )
    assert rollback_calls[0]["target_version"] == 1

    # 管理部门负责人可编辑，但不可发布。
    manager = TestClient(_admin_app(_user(ROLE_MANAGER, dept_code=AUDIT_DEPT_CODE)))
    saved = manager.put(
        "/admin/prompt-policy/draft",
        json={
            "company_core_policy": "规则",
            "default_persona": "人格",
            "expected_revision": 4,
        },
    )
    assert saved.json()["data"]["draft"]["revision"] == 4
    denied_publish = manager.post(
        "/admin/prompt-policy/publish", json={"expected_revision": 4}
    )
    assert denied_publish.json()["code"] == 10008
    denied_rollback = manager.post(
        "/admin/prompt-policy/rollback/1",
        json={"expected_current_version": 2},
    )
    assert denied_rollback.json()["code"] == 10008
    assert len(rollback_calls) == 1

    # 管理部门审计员只能查看。
    auditor = TestClient(_admin_app(_user(ROLE_AUDITOR, dept_code=AUDIT_DEPT_CODE)))
    assert (
        auditor.get("/admin/prompt-policy").json()["data"]["published"]["version"] == 2
    )
    assert (
        auditor.put(
            "/admin/prompt-policy/draft",
            json={
                "company_core_policy": "规则",
                "default_persona": "人格",
                "expected_revision": 4,
            },
        ).json()["code"]
        == 10008
    )


@pytest.mark.parametrize(
    ("role", "dept_code", "allowed"),
    [
        (ROLE_PRESIDENT, OTHER_DEPT, (True, True, False, True, True)),
        (ROLE_DEVELOPER, OTHER_DEPT, (True, True, True, True, True)),
        ("admin", OTHER_DEPT, (True, True, True, True, True)),
        (ROLE_MANAGER, AUDIT_DEPT_CODE, (True, True, True, False, False)),
        (ROLE_AUDITOR, AUDIT_DEPT_CODE, (True, True, False, False, False)),
        (ROLE_MANAGER, OTHER_DEPT, (False, False, False, False, False)),
        (ROLE_AUDITOR, OTHER_DEPT, (False, False, False, False, False)),
        (ROLE_EMPLOYEE, AUDIT_DEPT_CODE, (False, False, False, False, False)),
    ],
)
def test_every_prompt_policy_route_matches_the_exact_rbac_matrix(
    monkeypatch, role, dept_code, allowed
):
    draft = {
        "company_core_policy": "规则",
        "default_persona": "人格",
        "content_hash": "sha256:" + "a" * 64,
        "revision": 4,
    }
    published = {"version": 2, "content_hash": "sha256:" + "b" * 64}
    monkeypatch.setattr(
        "router.admin.prompt_policy.get_admin_state",
        AsyncMock(return_value={"published": published, "draft": draft}),
    )
    monkeypatch.setattr(
        "router.admin.prompt_policy.get_history",
        AsyncMock(return_value={"items": [], "events": []}),
    )
    monkeypatch.setattr(
        "router.admin.prompt_policy.save_draft", AsyncMock(return_value=draft)
    )
    monkeypatch.setattr(
        "router.admin.prompt_policy.publish_draft", AsyncMock(return_value=published)
    )
    monkeypatch.setattr(
        "router.admin.prompt_policy.rollback", AsyncMock(return_value=published)
    )

    client = TestClient(_admin_app(_user(role, dept_code=dept_code)))
    responses = [
        client.get("/admin/prompt-policy"),
        client.get("/admin/prompt-policy/history"),
        client.put(
            "/admin/prompt-policy/draft",
            json={
                "company_core_policy": "规则",
                "default_persona": "人格",
                "expected_revision": 4,
            },
        ),
        client.post(
            "/admin/prompt-policy/publish", json={"expected_revision": 4}
        ),
        client.post(
            "/admin/prompt-policy/rollback/1",
            json={"expected_current_version": 2},
        ),
    ]

    assert [response.json()["code"] == 0 for response in responses] == list(allowed)


def test_admin_route_returns_http_409_for_a_stale_draft(monkeypatch):
    async def stale_save(**_kwargs):
        raise policy.PromptPolicyConflictError(
            "草稿已被其他人更新，请刷新后重试",
            data={"current_revision": 9},
        )

    monkeypatch.setattr("router.admin.prompt_policy.save_draft", stale_save)
    response = TestClient(_admin_app(_user(ROLE_DEVELOPER))).put(
        "/admin/prompt-policy/draft",
        json={
            "company_core_policy": "规则",
            "default_persona": "人格",
            "expected_revision": 8,
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": 10005,
        "message": "草稿已被其他人更新，请刷新后重试",
        "data": {"current_revision": 9},
    }


def test_revoked_old_jwt_cannot_write_any_prompt_policy_surface(monkeypatch):
    """角色/部门调整撤销旧会话后，三条策略写路径都必须在服务调用前拒绝。"""
    from router.admin import prompt_policy as prompt_policy_router
    from utils import dependencies

    is_revoked = AsyncMock(return_value=True)
    monkeypatch.setattr(dependencies.token_manager, "is_token_revoked", is_revoked)
    save = AsyncMock()
    publish = AsyncMock()
    rollback_ = AsyncMock()
    monkeypatch.setattr(prompt_policy_router, "save_draft", save)
    monkeypatch.setattr(prompt_policy_router, "publish_draft", publish)
    monkeypatch.setattr(prompt_policy_router, "rollback", rollback_)

    client = TestClient(_admin_app_with_live_auth())
    headers = {"Authorization": "Bearer revoked-after-role-change"}
    responses = [
        client.put(
            "/admin/prompt-policy/draft",
            headers=headers,
            json={
                "company_core_policy": "规则",
                "default_persona": "人格",
                "expected_revision": 4,
            },
        ),
        client.post(
            "/admin/prompt-policy/publish",
            headers=headers,
            json={"expected_revision": 4},
        ),
        client.post(
            "/admin/prompt-policy/rollback/1",
            headers=headers,
            json={"expected_current_version": 2},
        ),
    ]

    assert [response.json()["code"] for response in responses] == [10002] * 3
    assert is_revoked.await_count == 3
    save.assert_not_awaited()
    publish.assert_not_awaited()
    rollback_.assert_not_awaited()


# ── 公共 effective endpoint ──────────────────────────────────────────────


def _effective_app(*, authenticated: bool) -> FastAPI:
    from router.v1.prompt_policy import router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/v1")
    if authenticated:
        app.dependency_overrides[require_caller] = lambda: Caller(
            uid="u-employee",
            username="employee",
            role=ROLE_EMPLOYEE,
            dept_id=7,
            dept_code=OTHER_DEPT,
            auth_type="apikey",
            api_key_id=1,
        )
    return app


def test_effective_policy_requires_authentication():
    response = TestClient(_effective_app(authenticated=False)).get(
        "/v1/prompt-policy/effective"
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "gateway_auth_failed"


def test_effective_policy_empty_database_returns_404_for_bundled_fallback(
    monkeypatch,
):
    async def no_published_policy():
        return None

    monkeypatch.setattr(
        "router.v1.prompt_policy.get_effective_policy", no_published_policy
    )
    response = TestClient(_effective_app(authenticated=True)).get(
        "/v1/prompt-policy/effective"
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "code": 10011,
        "message": "网关尚未发布提示词策略，请使用安装包内置基线",
        "data": {
            "reason": "prompt_policy_unpublished",
            "fallback": "bundled",
        },
    }


def test_effective_policy_is_no_store_and_returns_only_the_published_package(
    monkeypatch,
):
    published = {
        "version": 7,
        "company_core_policy": "规则",
        "default_persona": "人格",
        "content_hash": "sha256:" + "a" * 64,
        "published_at": "2026-09-17T12:00:00",
        "published_by": "developer",
        "published_by_uid": "u-dev",
        "source_version": 5,
    }

    async def fake_effective():
        return dict(published)

    monkeypatch.setattr("router.v1.prompt_policy.get_effective_policy", fake_effective)

    response = TestClient(_effective_app(authenticated=True)).get(
        "/v1/prompt-policy/effective"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "version": 7,
        "content_hash": "sha256:" + "a" * 64,
        "company_core_policy": "规则",
        "default_persona": "人格",
        "published_at": "2026-09-17T12:00:00",
    }


def test_effective_policy_accepts_the_desktop_bearer_api_key_path(monkeypatch):
    called = {}

    async def fake_from_api_key(plain: str):
        called["plain"] = plain
        return Caller(
            uid="u-employee",
            username="employee",
            role=ROLE_EMPLOYEE,
            dept_id=7,
            dept_code=OTHER_DEPT,
            auth_type="apikey",
            api_key_id=1,
        )

    async def fake_effective():
        return {
            "version": 1,
            "company_core_policy": "规则",
            "default_persona": "人格",
            "content_hash": "sha256:" + "b" * 64,
            "published_at": "2026-09-17T12:00:00",
            "published_by": "developer",
            "published_by_uid": "u-dev",
            "source_version": None,
        }

    monkeypatch.setattr("utils.gateway_auth._caller_from_api_key", fake_from_api_key)
    monkeypatch.setattr("router.v1.prompt_policy.get_effective_policy", fake_effective)

    app = _effective_app(authenticated=False)
    token = "sk-llano-" + "a" * 40
    response = TestClient(app).get(
        "/v1/prompt-policy/effective",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert called["plain"] == token
    assert response.json()["version"] == 1
