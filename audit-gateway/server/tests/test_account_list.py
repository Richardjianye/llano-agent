"""账号列表分页与最近真实请求的接口契约。"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from model.response import UserInfo
from utils.dependencies import require_login


def _compiled(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def _president() -> UserInfo:
    return UserInfo(
        id=1,
        uid="operator",
        username="admin",
        permission="president",
        dept_id=1,
        dept_code="ZCB",
        permissions=[],
    )


@pytest.fixture
def client(monkeypatch):
    calls = {}

    async def _users(**kwargs):
        calls["list"] = kwargs
        return [
            SimpleNamespace(
                account_kind="internal",
                uid="active-user",
                username="活跃用户",
                realname=None,
                nickname=None,
                permission="user",
                dept_id=2,
                status=1,
                last_login_at=datetime(2026, 9, 9, 9, 0, 0),
            ),
            SimpleNamespace(
                account_kind="internal",
                uid="idle-user",
                username="未使用用户",
                realname=None,
                nickname=None,
                permission="user",
                dept_id=2,
                status=1,
                last_login_at=None,
            ),
        ]

    async def _count(**kwargs):
        calls["count"] = kwargs
        return 283

    async def _activity(user_uids):
        calls["activity"] = user_uids
        return {"active-user": datetime(2026, 9, 10, 8, 30, 0)}

    monkeypatch.setattr("router.admin.account.list_users", _users)
    monkeypatch.setattr("router.admin.account.count_users", _count)
    monkeypatch.setattr("router.admin.account.latest_chat_request_at", _activity)

    from router.admin.account import router

    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[require_login] = _president
    return TestClient(app), calls


def test_account_list_is_paginated_and_returns_real_request_activity(client):
    http, calls = client

    response = http.get(
        "/admin/account/list",
        params={
            "keyword": "用户",
            "dept_id": 2,
            "role": "user",
            "limit": 20,
            "offset": 40,
            "sort_by": "last_request_at",
            "sort_order": "desc",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 283
    assert data["items"][0]["last_request_at"] == "2026-09-10T08:30:00"
    assert data["items"][1]["last_request_at"] is None
    assert calls["list"] == {
        "dept_id": 2,
        "role": "user",
        "keyword": "用户",
        "limit": 20,
        "offset": 40,
        "sort_by": "last_request_at",
        "sort_order": "desc",
    }
    assert calls["count"] == {
        "dept_id": 2,
        "role": "user",
        "keyword": "用户",
    }
    assert calls["activity"] == ["active-user", "idle-user"]


def test_count_query_uses_the_same_keyword_filter_as_the_page():
    from db.selects.user import build_user_count_query

    sql = _compiled(
        build_user_count_query(dept_id=2, role="user", keyword="阿乙")
    )

    assert "users.dept_id = 2" in sql
    assert "users.permission = 'user'" in sql
    assert "users.username LIKE '%阿乙%'" in sql
    assert "users.realname LIKE '%阿乙%'" in sql


def test_recent_activity_counts_only_real_chat_requests():
    from db.selects.user import build_latest_chat_request_query

    sql = _compiled(build_latest_chat_request_query(["u1", "u2"]))

    assert "usage_records.user_uid IN ('u1', 'u2')" in sql
    assert "usage_records.call_kind = 'chat'" in sql
    assert "usage_records.status" not in sql
    assert "GROUP BY usage_records.user_uid" in sql


def test_user_list_sort_is_global_and_keeps_empty_activity_last():
    from db.selects.user import build_user_list_query

    sql = _compiled(build_user_list_query(
        sort_by="last_request_at",
        sort_order="desc",
    ))

    assert "max(usage_records.created_at)" in sql
    assert "usage_records.call_kind = 'chat'" in sql
    assert "ORDER BY CASE WHEN" in sql
    assert "DESC, users.id ASC" in sql


def test_user_list_sort_uses_allowlist_and_department_name():
    from db.selects.user import build_user_list_query

    dept_sql = _compiled(build_user_list_query(sort_by="dept_id"))
    rejected_sql = _compiled(build_user_list_query(sort_by="id desc; drop table users"))

    assert "LEFT OUTER JOIN departments" in dept_sql
    assert "departments.name ASC" in dept_sql
    assert "drop table" not in rejected_sql.lower()
    assert "users.id ASC" in rejected_sql
