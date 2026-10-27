"""白名单：成员搜索、部门继承、批量授权、按模型/按成员视图（2026-10-19）

用真实的 SQLAlchemy 查询打一个 sqlite 库，走真实路由（只 override 登录态），
不 mock 判定逻辑 —— 这几处的 bug 都出在「查询/拼装」那一层，mock 掉就测不到了：

- 选成员只拉了前 200 个账号（生产 290 人），第 201 个起搜不到
- dept 规则只比本部门 ID，上级部门开放的模型下级用不了（源贺 / 视觉中心）
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import (Base, Department, ModelInfo, ModelWhitelist,
                            UpstreamProvider, User, now)
from model.response import UserInfo
from services.member_search import (MemberQuery, build_search_query,
                                    escape_like)
from services.model_access import AccessSubject, list_available_models
from services.model_grants import (GrantTarget, plan_batch_grants)
from services.model_access import WhitelistRule
from utils.dependencies import require_login
from utils.middleware import register_exception_handlers

TABLES = [Department.__table__, User.__table__, UpstreamProvider.__table__,
          ModelInfo.__table__, ModelWhitelist.__table__]

# 视觉中心(9) → 设计创意部(30) → 创意一组(31)；国内营销中心(5) 是另一棵树
DEPTS = [(9, "视觉中心", None), (30, "设计创意部", 9), (31, "创意一组", 30), (5, "国内营销中心", None)]
GPT, FLASH, IMAGE = 16, 7, 11


def _president() -> UserInfo:
    return UserInfo(id=1, uid="u-president", username="boss", permission="president",
                    dept_id=None, dept_code=None, permissions=[])


@pytest.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'grants.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def factory():
        return maker

    import db as db_pkg
    import db.inserts.catalog as ins
    import db.selects.catalog as sel
    import db.updates.catalog as upd
    import db.selects.department as dep
    import db.selects.user as usr
    for mod in (db_pkg, ins, sel, upd, dep, usr):
        monkeypatch.setattr(mod, "init_async_session", factory)

    async with maker() as session:
        for dept_id, name, parent in DEPTS:
            session.add(Department(id=dept_id, code=f"d{dept_id}", name=name, parent_id=parent, status=1))
        # 300 个人，源贺排在第 260 位 —— 原来 limit=200 的选人框里根本没有他
        for i in range(1, 301):
            name = "源贺" if i == 260 else f"员工{i:03d}"
            dept = 30 if i == 260 else (31 if i == 261 else 5)
            session.add(User(id=i, uid=f"u{i:03d}", username=f"user{i:03d}", realname=name,
                             nickname=name, password="x", permission="employee",
                             dept_id=dept, status=0 if i == 299 else 1))
        session.add(UpstreamProvider(id=1, code="p", name="p", base_url="http://x", status=1))
        for model_id, name in ((GPT, "gpt-6.1-sol"), (FLASH, "deepseek-flash"), (IMAGE, "gpt-image-2")):
            session.add(ModelInfo(id=model_id, provider_id=1, model_name=name, display_name=name, status=1))
        session.add(ModelWhitelist(scope="global", scope_value="", model_id=FLASH, allowed=True))
        session.add(ModelWhitelist(scope="dept", scope_value="9", model_id=GPT, allowed=True))
        await session.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
def client(db):
    from router.admin import router as admin_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_router)
    app.dependency_overrides[require_login] = _president
    return TestClient(app)


# ---------------- 成员搜索 ----------------


def test_成员搜索覆盖全量而不是前200人(client):
    body = client.get("/admin/member/search", params={"keyword": "源贺"}).json()
    assert body["code"] == 0, body
    data = body["data"]
    assert data["total"] == 1
    hit = data["items"][0]
    assert hit["uid"] == "u260"
    assert hit["dept_path"] == "视觉中心 / 设计创意部"
    assert "password" not in hit and "dingtalk_user_id" not in hit


def test_成员搜索分页与总数一致(client):
    first = client.get("/admin/member/search", params={"limit": 100}).json()["data"]
    assert first["total"] == 299  # 默认只要在职，299 号停用
    assert len(first["items"]) == 100 and first["has_more"] is True
    last = client.get("/admin/member/search", params={"limit": 100, "offset": 200}).json()["data"]
    assert len(last["items"]) == 99 and last["has_more"] is False
    everyone = client.get("/admin/member/search", params={"status": "all", "limit": 1}).json()["data"]
    assert everyone["total"] == 300


def test_成员搜索按部门含下级过滤(client):
    data = client.get("/admin/member/search", params={"dept_ids": "9"}).json()["data"]
    assert {i["uid"] for i in data["items"]} == {"u260", "u261"}
    only = client.get("/admin/member/search",
                      params={"dept_ids": "9", "include_descendants": "false"}).json()["data"]
    assert only["total"] == 0


def test_成员搜索按uid取回已选值(client):
    data = client.get("/admin/member/search", params={"uids": "u260,u001", "limit": 10}).json()["data"]
    assert {i["uid"] for i in data["items"]} == {"u260", "u001"}


def test_成员搜索拒绝非法参数(client):
    assert client.get("/admin/member/search", params={"roles": "root"}).json()["code"] != 0
    assert client.get("/admin/member/search", params={"dept_ids": "1;drop"}).json()["code"] != 0
    assert client.get("/admin/member/search", params={"limit": 101}).json()["code"] != 0


def test_成员搜索需要权限(client):
    client.app.dependency_overrides[require_login] = lambda: UserInfo(
        id=2, uid="u-emp", username="e", permission="employee", dept_id=5, dept_code=None, permissions=[])
    assert client.get("/admin/member/search", params={"keyword": "源"}).json()["code"] == 10008


def test_关键字里的通配符按字面匹配():
    assert escape_like("50%_a\\b") == "50\\%\\_a\\\\b"
    sql = str(build_search_query(MemberQuery(keyword="_")).compile(compile_kwargs={"literal_binds": True}))
    assert "ESCAPE" in sql.upper()


# ---------------- 部门继承（真实查询链） ----------------


async def test_上级部门开放的模型下级成员可用(db):
    subject = AccessSubject(uid="u260", role="employee", dept_id=30)
    names = {m.model_name for m in await list_available_models(subject)}
    assert names == {"gpt-6.1-sol", "deepseek-flash"}


async def test_多级部门继承到顶级(db):
    subject = AccessSubject(uid="u261", role="employee", dept_id=31)
    assert "gpt-6.1-sol" in {m.model_name for m in await list_available_models(subject)}


async def test_其他部门拿不到(db):
    subject = AccessSubject(uid="u001", role="employee", dept_id=5)
    assert {m.model_name for m in await list_available_models(subject)} == {"deepseek-flash"}


# ---------------- 批量授权 ----------------


def test_批量计划去重并跳过已有条目_不覆盖拒绝():
    existing = [WhitelistRule("dept", "9", 1, allowed=False)]
    plan = plan_batch_grants(
        [1, 2, 2],
        [GrantTarget("dept", "9"), GrantTarget("dept", "9"), GrantTarget("role", "employee"),
         GrantTarget("global", "ignored")],
        existing,
    )
    created = {(t.scope, t.scope_value, m) for t, m in plan.to_create}
    assert created == {("role", "employee", 1), ("global", "", 1),
                       ("dept", "9", 2), ("role", "employee", 2), ("global", "", 2)}
    assert [(t.scope, t.scope_value, m, a) for t, m, a in plan.skipped] == [("dept", "9", 1, False)]


def test_批量授权多部门多角色多成员(client):
    body = client.post("/admin/catalog/whitelist/batch-create", json={
        "model_ids": [IMAGE, GPT],
        "targets": [
            {"scope": "dept", "scope_value": "5"},
            {"scope": "dept", "scope_value": "9"},
            {"scope": "role", "scope_value": "manager"},
            {"scope": "user", "scope_value": "u001"},
        ],
        "remark": "批量",
    }).json()
    assert body["code"] == 0, body
    # 2 模型 × 4 对象 = 8，其中 dept 9 × GPT 已存在
    assert body["data"]["created"] == 7
    assert body["data"]["skipped"] == [
        {"scope": "dept", "scope_value": "9", "model_id": GPT, "existing_allowed": True}]

    again = client.post("/admin/catalog/whitelist/batch-create", json={
        "model_ids": [IMAGE], "targets": [{"scope": "dept", "scope_value": "5"}]}).json()
    assert again["data"]["created"] == 0


def test_批量授权校验对象存在(client):
    for target in ({"scope": "dept", "scope_value": "404"},
                   {"scope": "user", "scope_value": "nobody"},
                   {"scope": "role", "scope_value": "root"}):
        body = client.post("/admin/catalog/whitelist/batch-create",
                           json={"model_ids": [GPT], "targets": [target]}).json()
        assert body["code"] != 0, target
    body = client.post("/admin/catalog/whitelist/batch-create",
                       json={"model_ids": [999], "targets": [{"scope": "global"}]}).json()
    assert body["code"] != 0


def test_批量删除(client):
    listing = client.get("/admin/catalog/whitelist/list").json()["data"]["items"]
    ids = [e["id"] for e in listing]
    body = client.post("/admin/catalog/whitelist/batch-delete", json={"ids": ids + [9999]}).json()
    assert body["data"]["deleted"] == len(ids)
    assert client.get("/admin/catalog/whitelist/list").json()["data"]["items"] == []


# ---------------- 视图 ----------------


def test_按模型看授权与实际可用人数(client):
    data = client.get("/admin/catalog/whitelist/overview").json()["data"]
    by_model = {i["model_id"]: i for i in data["items"]}
    gpt = by_model[GPT]
    assert gpt["grants"]["dept"] == [{"scope_value": "9", "label": "视觉中心", "allowed": True,
                                      "descendant_dept_count": 2}]
    # 视觉中心本级没人，设计创意部 1 人 + 创意一组 1 人
    assert gpt["effective_user_count"] == 2
    assert by_model[FLASH]["effective_user_count"] == data["active_user_count"] == 299
    assert by_model[IMAGE]["effective_user_count"] == 0


def test_按成员反查能用的模型和来源(client):
    data = client.get("/admin/catalog/whitelist/member-access/u260").json()["data"]
    assert data["member"]["dept_path"] == "视觉中心 / 设计创意部"
    by_model = {i["model_id"]: i for i in data["items"]}
    assert by_model[GPT]["allowed"] is True
    assert by_model[GPT]["allow_sources"] == [{
        "scope": "dept", "scope_value": "9", "label": "视觉中心", "allowed": True,
        "inherited_from_dept_id": 9}]
    assert by_model[FLASH]["allow_sources"][0]["scope"] == "global"
    assert client.get("/admin/catalog/whitelist/member-access/nobody").json()["code"] != 0


def test_反查结论与客户端拿到的模型一致(client, db):
    """视图和真实下发必须是同一套判定"""
    import asyncio
    data = client.get("/admin/catalog/whitelist/member-access/u261").json()["data"]
    shown = {i["model_id"] for i in data["items"] if i["allowed"]}
    served = asyncio.run(list_available_models(AccessSubject(uid="u261", role="employee", dept_id=31)))
    assert shown == {m.id for m in served}
