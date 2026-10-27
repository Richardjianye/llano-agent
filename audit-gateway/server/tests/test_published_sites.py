"""静态产物从上传到每个资源的真实 ASGI 访问链路。"""

import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import Department, PublishedSite, PublishedSiteVersion, User
from model.response import Response as GatewayResponse, UserInfo
from router import sites
from services import published_sites


def identity(uid: str, dept: str) -> UserInfo:
    return UserInfo(id=1, uid=uid, permission="employee", dept_code=dept)


def bundle(files: dict[str, bytes]) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return result.getvalue()


def test_storage_cannot_overlap_public_static(monkeypatch):
    public = str(published_sites.Path(published_sites.__file__).resolve().parents[1] / "static" / "sites")
    monkeypatch.setattr(published_sites, "get_settings",
                        lambda: SimpleNamespace(artifact_storage_path=public))
    with pytest.raises(HTTPException) as caught:
        published_sites.storage_root()
    assert caught.value.status_code == 503


@pytest.fixture
async def site_app(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (User.__table__, Department.__table__, PublishedSite.__table__,
                      PublishedSiteVersion.__table__):
            await connection.run_sync(table.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_maker():
        return maker

    monkeypatch.setattr(sites, "init_async_session", session_maker)
    monkeypatch.setattr(published_sites, "get_settings",
                        lambda: SimpleNamespace(artifact_storage_path=str(tmp_path)))
    async with maker() as session:
        session.add_all([
            User(uid="owner", username="owner", password="x", status=1),
            User(uid="viewer", username="viewer", password="x", status=1),
            Department(code="sales", name="销售部", status=1),
            Department(code="product", name="产品部", status=1),
        ])
        await session.commit()

    current = {"user": identity("owner", "sales")}

    async def viewer(*, token, request):
        return current["user"] or GatewayResponse(code=10002)

    monkeypatch.setattr(sites, "get_current_user", viewer)
    app = FastAPI()
    app.include_router(sites.router)
    def signed_in():
        if current["user"] is None:
            raise HTTPException(401, "请登录")
        return current["user"]
    app.dependency_overrides[sites.site_login] = signed_in
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, current, maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_html_preview_publish_acl_and_withdraw(site_app):
    client, current, _ = site_app
    created = await client.post("/sites", data={"title": "报表"},
                                files={"upload": ("report.html", b"<h1>v1</h1>", "text/html")})
    assert created.status_code == 200, created.text
    site = created.json()
    site_id = site["id"]
    preview = await client.get(f"/sites/{site_id}/preview/1/index.html")
    assert preview.text == "<h1>v1</h1>"
    assert "sandbox allow-same-origin" in preview.headers["content-security-policy"]
    assert "script-src 'none'" in preview.headers["content-security-policy"]
    assert (await client.get(site["url"])).status_code == 404

    updated = await client.put(f"/sites/{site_id}", json={"title": "报表", "visibility": "users",
                                                        "allowed_users": ["viewer"], "allowed_depts": []})
    assert updated.status_code == 200
    assert (await client.post(f"/sites/{site_id}/publish", json={"version": 1})).status_code == 200
    current["user"] = identity("viewer", "product")
    assert (await client.get(site["url"])).text == "<h1>v1</h1>"
    assert (await client.get(f"/sites/{site_id}/preview/1/index.html")).status_code == 404
    current["user"] = identity("stranger", "product")
    assert (await client.get(site["url"])).status_code == 404
    current["user"] = None
    assert (await client.get(site["url"])).status_code == 404
    assert (await client.get(f"/sites/{site_id}/preview/1/index.html")).status_code == 401
    assert (await client.get("/sites")).status_code == 401
    current["user"] = identity("owner", "sales")
    assert (await client.post(f"/sites/{site_id}/withdraw")).status_code == 200
    current["user"] = identity("viewer", "product")
    assert (await client.get(site["url"])).status_code == 404


@pytest.mark.asyncio
async def test_zip_assets_department_change_and_rollback(site_app):
    client, current, maker = site_app
    first = bundle({"index.html": b'<link rel="stylesheet" href="assets/site.css">',
                    "assets/site.css": b"body{color:red}"})
    created = await client.post("/sites", data={"title": "网站"},
                                files={"upload": ("site.zip", first, "application/zip")})
    assert created.status_code == 200, created.text
    site_id = created.json()["id"]
    assert (await client.get(f"/sites/{site_id}/preview/1/assets/site.css")).text == "body{color:red}"
    second = await client.post(f"/sites/{site_id}/versions",
                               files={"upload": ("new.html", b"<h1>v2</h1>", "text/html")})
    assert second.json()["version"] == 2
    await client.put(f"/sites/{site_id}", json={"title": "网站", "visibility": "depts",
                                                 "allowed_users": [], "allowed_depts": ["sales"]})
    await client.post(f"/sites/{site_id}/publish", json={"version": 1})
    current["user"] = identity("viewer", "sales")
    assert (await client.get(f"/sites/{site_id}/view/assets/site.css")).text == "body{color:red}"
    async with maker() as session:
        department = (await session.execute(
            select(Department).where(Department.code == "sales"))).scalar_one()
        department.status = 0
        await session.commit()
    assert (await client.get(f"/sites/{site_id}/view/assets/site.css")).status_code == 404
    async with maker() as session:
        department = (await session.execute(
            select(Department).where(Department.code == "sales"))).scalar_one()
        department.status = 1
        await session.commit()
    current["user"].dept_code = "product"
    assert (await client.get(f"/sites/{site_id}/view/assets/site.css")).status_code == 404
    current["user"] = identity("owner", "sales")
    await client.post(f"/sites/{site_id}/publish", json={"version": 2})
    assert (await client.get(f"/sites/{site_id}/view/index.html")).text == "<h1>v2</h1>"
    assert (await client.get(f"/sites/{site_id}/view/assets/site.css")).status_code == 404
    await client.post(f"/sites/{site_id}/publish", json={"version": 1})
    assert (await client.get(f"/sites/{site_id}/view/assets/site.css")).status_code == 200
    await client.put(f"/sites/{site_id}", json={"title": "网站", "visibility": "public",
                                                 "allowed_users": [], "allowed_depts": []})
    current["user"] = None
    public = await client.get(f"/sites/{site_id}/view/index.html")
    assert public.status_code == 200
    assert "sandbox allow-scripts" in public.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_rejects_zip_traversal_and_bad_acl(site_app):
    client, _, _ = site_app
    malicious = bundle({"index.html": b"ok", "../escape.html": b"bad"})
    result = await client.post("/sites", data={"title": "坏包"},
                               files={"upload": ("bad.zip", malicious, "application/zip")})
    assert result.status_code == 400
    duplicate = bundle({"index.html": b"ok", "Assets/site.css": b"a", "assets/site.css": b"b"})
    result = await client.post("/sites", data={"title": "重复"},
                               files={"upload": ("same.zip", duplicate, "application/zip")})
    assert result.status_code == 400
    valid = await client.post("/sites", data={"title": "正常"},
                              files={"upload": ("index.html", b"ok", "text/html")})
    site_id = valid.json()["id"]
    invalid = await client.put(f"/sites/{site_id}", json={"title": "正常", "visibility": "users",
                                                        "allowed_users": ["missing"], "allowed_depts": []})
    assert invalid.status_code == 400
    assert (await client.get(f"/sites/{site_id}/preview/1/missing.css")).status_code == 404


@pytest.mark.asyncio
async def test_role_demo_component_and_data_permissions_are_separate(site_app):
    client, current, _ = site_app
    created = await client.post("/sites", data={"title": "权限示例"},
                                files={"upload": ("index.html", b"<h1>demo</h1>", "text/html")})
    site_id = created.json()["id"]
    base = f"/sites/{site_id}/demo"
    policy = (await client.get(f"{base}/policy")).json()["policy"]
    assert policy["components"]["welcome"]
    await client.put(f"/sites/{site_id}", json={"title": "权限示例", "visibility": "users",
                                                "allowed_users": ["viewer"], "allowed_depts": []})
    await client.post(f"/sites/{site_id}/publish", json={"version": 1})

    current["user"] = identity("viewer", "product")
    capability = (await client.get(f"{base}/capabilities")).json()
    assert capability["components"] == ["welcome"]
    assert capability["data"] == []
    assert (await client.get(f"{base}/data/team")).status_code == 404
    assert (await client.get(f"{base}/data/finance")).status_code == 404
    assert (await client.get(f"{base}/policy")).status_code == 404
    assert (await client.put(f"{base}/policy", json=policy)).status_code == 404

    current["user"] = identity("owner", "sales")
    policy["components"]["team"].append("employee")
    assert (await client.put(f"{base}/policy", json=policy)).status_code == 200
    current["user"] = identity("viewer", "product")
    capability = (await client.get(f"{base}/capabilities")).json()
    assert capability["components"] == ["team", "welcome"]
    assert capability["data"] == []
    assert (await client.get(f"{base}/data/team")).status_code == 404

    current["user"] = identity("owner", "sales")
    policy["data"]["team"].append("employee")
    assert (await client.put(f"{base}/policy", json=policy)).status_code == 200
    current["user"] = identity("viewer", "product")
    assert (await client.get(f"{base}/data/team")).json()["completed"] == 12
    assert (await client.get(f"{base}/data/finance")).status_code == 404
    current["user"] = identity("owner", "sales")
    await client.put(f"/sites/{site_id}", json={"title": "权限示例", "visibility": "private",
                                                "allowed_users": [], "allowed_depts": []})
    current["user"] = identity("viewer", "product")
    assert (await client.get(f"{base}/capabilities")).status_code == 404
    assert (await client.get(f"{base}/data/team")).status_code == 404
    current["user"] = None
    assert (await client.get(f"{base}/capabilities")).status_code == 401
