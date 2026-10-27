"""静态网站产物：上传、版本、访问范围与逐资源受控展示。"""

import json
import uuid

from db import init_async_session
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from loguru import logger
from model.database import Department, PublishedSite, PublishedSiteVersion, User
from model.response import Response as GatewayResponse, UserInfo
from pydantic import BaseModel, Field
from services.published_sites import (MAX_UPLOAD_BYTES, allowed, check_acl_shape,
                                      discard_version, file_response, storage_root,
                                      unpack_upload, write_version)
from services.site_demo import (COMPONENTS, DATASETS, EXAMPLE_DATA, ROLES, granted,
                                load_policy, save_policy, validate_policy)
from sqlalchemy import select
from utils.dependencies import get_current_user

router = APIRouter(prefix="/sites", tags=["静态网站产物"])


async def site_login(request: Request) -> UserInfo:
    """本组接口使用真实 HTTP 401，供网站前端刷新登录态。"""
    current = await get_current_user(token=request.cookies.get("access_token"), request=request)
    if isinstance(current, GatewayResponse):
        raise HTTPException(401, "请先登录")
    return current


def _summary(site: PublishedSite) -> dict:
    return {"id": site.id, "title": site.title, "owner_uid": site.owner_uid,
            "visibility": site.visibility, "allowed_users": json.loads(site.allowed_users),
            "allowed_depts": json.loads(site.allowed_depts), "status": site.status,
            "latest_version": site.latest_version, "published_version": site.published_version,
            "created_at": site.created_at.isoformat(), "updated_at": site.updated_at.isoformat(),
            "url": f"/sites/{site.id}/view/index.html"}


def _owned(site: PublishedSite | None, user: UserInfo) -> PublishedSite:
    if site is None or site.owner_uid != user.uid:
        raise HTTPException(404, "网站不存在")
    return site


async def _existing_acl(session, visibility: str, users: list[str], depts: list[str]) -> None:
    check_acl_shape(visibility, users, depts)
    if users:
        actual = set((await session.execute(select(User.uid).where(
            User.uid.in_(users), User.status == 1))).scalars().all())
        if actual != set(users):
            raise HTTPException(400, "指定用户包含不存在或停用账号")
    if depts:
        actual = set((await session.execute(select(Department.code).where(
            Department.code.in_(depts), Department.status == 1))).scalars().all())
        if actual != set(depts):
            raise HTTPException(400, "指定部门包含不存在或停用部门")


@router.get("")
async def my_sites(user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        sites = (await session.execute(select(PublishedSite).where(
            PublishedSite.owner_uid == user.uid).order_by(PublishedSite.created_at.desc()))).scalars().all()
        return [_summary(site) for site in sites]


@router.get("/subjects")
async def search_subjects(q: str = Query("", max_length=64),
                          _: UserInfo = Depends(site_login)):
    """分享目标只返回最少的稳定标识和展示名，不下发电话或邮箱。"""
    keyword = q.strip()
    maker = await init_async_session()
    async with maker() as session:
        departments = (await session.execute(select(Department).where(
            Department.status == 1,
            (Department.code.like(f"%{keyword}%") | Department.name.like(f"%{keyword}%"))
        ).limit(30))).scalars().all()
        users = []
        if len(keyword) >= 2:
            users = (await session.execute(select(User).where(
                User.status == 1,
                (User.username.like(f"%{keyword}%") | User.realname.like(f"%{keyword}%")
                 | User.nickname.like(f"%{keyword}%"))
            ).limit(30))).scalars().all()
    return {"users": [{"value": item.uid, "title": item.realname or item.nickname or item.username}
                      for item in users if item.uid],
            "depts": [{"value": item.code, "title": item.name} for item in departments]}


@router.post("")
async def create_site(title: str = Form(), upload: UploadFile = File(),
                      user: UserInfo = Depends(site_login)):
    title = title.strip()
    if not 1 <= len(title) <= 128:
        raise HTTPException(400, "网站名称需为 1～128 个字符")
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    files = unpack_upload(upload.filename or "", content)
    root = storage_root()
    site_id = str(uuid.uuid4())
    maker = await init_async_session()
    site = PublishedSite(id=site_id, title=title, owner_uid=user.uid,
                         visibility="private", allowed_users="[]", allowed_depts="[]",
                         status="draft", latest_version=1)
    version = PublishedSiteVersion(site_id=site_id, version=1,
                                   source_kind="zip" if (upload.filename or "").lower().endswith(".zip") else "html",
                                   files=json.dumps(sorted(files), ensure_ascii=False),
                                   bytes_total=sum(map(len, files.values())), created_by=user.uid)
    async with maker() as session:
        written = False
        try:
            write_version(root, site_id, 1, files)
            written = True
            session.add_all([site, version])
            await session.commit()
        except Exception:
            await session.rollback()
            if written:
                discard_version(root, site_id, 1)
            raise
    logger.info("静态网站已创建: id={} owner={} version=1", site_id, user.uid)
    return _summary(site)


@router.post("/{site_id}/versions")
async def add_version(site_id: str, upload: UploadFile = File(),
                      user: UserInfo = Depends(site_login)):
    files = unpack_upload(upload.filename or "", await upload.read(MAX_UPLOAD_BYTES + 1))
    root = storage_root()
    maker = await init_async_session()
    async with maker() as session:
        site = _owned(await session.get(PublishedSite, site_id, with_for_update=True), user)
        number = site.latest_version + 1
        version = PublishedSiteVersion(site_id=site_id, version=number,
                                       source_kind="zip" if (upload.filename or "").lower().endswith(".zip") else "html",
                                       files=json.dumps(sorted(files), ensure_ascii=False),
                                       bytes_total=sum(map(len, files.values())), created_by=user.uid)
        written = False
        try:
            write_version(root, site_id, number, files)
            written = True
            site.latest_version = number
            session.add(version)
            await session.commit()
        except Exception:
            await session.rollback()
            if written:
                discard_version(root, site_id, number)
            raise
    logger.info("静态网站新版本: id={} owner={} version={}", site_id, user.uid, number)
    return {"site_id": site_id, "version": number,
            "preview_url": f"/sites/{site_id}/preview/{number}/index.html"}


@router.get("/{site_id}/versions")
async def list_versions(site_id: str, user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        _owned(await session.get(PublishedSite, site_id), user)
        versions = (await session.execute(select(PublishedSiteVersion).where(
            PublishedSiteVersion.site_id == site_id).order_by(PublishedSiteVersion.version.desc()))).scalars().all()
        return [{"version": item.version, "source_kind": item.source_kind,
                 "files": json.loads(item.files), "bytes_total": item.bytes_total,
                 "created_at": item.created_at.isoformat(),
                 "preview_url": f"/sites/{site_id}/preview/{item.version}/index.html"}
                for item in versions]


class SiteSettings(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    visibility: str
    allowed_users: list[str] = Field(default_factory=list)
    allowed_depts: list[str] = Field(default_factory=list)


@router.put("/{site_id}")
async def update_site(site_id: str, body: SiteSettings, user: UserInfo = Depends(site_login)):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "网站名称不能为空")
    maker = await init_async_session()
    async with maker() as session:
        site = _owned(await session.get(PublishedSite, site_id, with_for_update=True), user)
        selected_users = body.allowed_users if body.visibility == "users" else []
        selected_depts = body.allowed_depts if body.visibility == "depts" else []
        await _existing_acl(session, body.visibility, selected_users, selected_depts)
        site.title = title
        site.visibility = body.visibility
        site.allowed_users = json.dumps(selected_users)
        site.allowed_depts = json.dumps(selected_depts)
        await session.commit()
        await session.refresh(site)
        result = _summary(site)
    logger.info("静态网站访问范围已更新: id={} owner={} visibility={}", site_id, user.uid, body.visibility)
    return result


class PublishVersion(BaseModel):
    version: int = Field(ge=1)


@router.post("/{site_id}/publish")
async def publish_site(site_id: str, body: PublishVersion, user: UserInfo = Depends(site_login)):
    root = storage_root()
    maker = await init_async_session()
    async with maker() as session:
        site = _owned(await session.get(PublishedSite, site_id, with_for_update=True), user)
        version = (await session.execute(select(PublishedSiteVersion).where(
            PublishedSiteVersion.site_id == site_id, PublishedSiteVersion.version == body.version))).scalar_one_or_none()
        if version is None or not (root / site_id / str(body.version) / "index.html").is_file():
            raise HTTPException(404, "网站版本不存在")
        site.published_version = body.version
        site.status = "published"
        await session.commit()
        await session.refresh(site)
        result = _summary(site)
    logger.info("静态网站已发布: id={} owner={} version={}", site_id, user.uid, body.version)
    return result


@router.post("/{site_id}/withdraw")
async def withdraw_site(site_id: str, user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        site = _owned(await session.get(PublishedSite, site_id, with_for_update=True), user)
        site.status = "withdrawn"
        await session.commit()
        await session.refresh(site)
        result = _summary(site)
    logger.info("静态网站已撤回: id={} owner={}", site_id, user.uid)
    return result


async def _viewer(request: Request):
    current = await get_current_user(token=request.cookies.get("access_token"), request=request)
    return None if isinstance(current, GatewayResponse) else current


@router.get("/{site_id}/preview/{version}/{asset_path:path}")
async def preview_resource(site_id: str, version: int, asset_path: str,
                           user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        _owned(await session.get(PublishedSite, site_id), user)
        record = (await session.execute(select(PublishedSiteVersion).where(
            PublishedSiteVersion.site_id == site_id, PublishedSiteVersion.version == version))).scalar_one_or_none()
        if record is None:
            raise HTTPException(404, "网站版本不存在")
        return file_response(storage_root(), site_id, version, json.loads(record.files), asset_path)


@router.get("/{site_id}/view/{asset_path:path}")
async def published_resource(site_id: str, asset_path: str, request: Request):
    user = await _viewer(request)
    maker = await init_async_session()
    async with maker() as session:
        site = await session.get(PublishedSite, site_id)
        if site and user and site.visibility == "depts" and user.uid != site.owner_uid:
            department = (await session.execute(select(Department.id).where(
                Department.code == user.dept_code, Department.status == 1))).scalar_one_or_none()
            if department is None:
                user = None
        if site is None or not allowed(site, user):
            raise HTTPException(404, "网站不存在或无权访问")
        record = (await session.execute(select(PublishedSiteVersion).where(
            PublishedSiteVersion.site_id == site_id,
            PublishedSiteVersion.version == site.published_version))).scalar_one_or_none()
        if record is None:
            raise HTTPException(404, "网站版本不存在")
        return file_response(storage_root(), site_id, record.version,
                             json.loads(record.files), asset_path,
                             public=site.visibility == "public")


async def _demo_site(session, site_id: str, user: UserInfo) -> PublishedSite:
    """The demo is login-only and inherits the parent site's publication ACL."""
    site = await session.get(PublishedSite, site_id)
    if site is None:
        raise HTTPException(404, "网站不存在")
    if user.uid == site.owner_uid:
        return site
    if site.visibility == "depts":
        department = (await session.execute(select(Department.id).where(
            Department.code == user.dept_code, Department.status == 1))).scalar_one_or_none()
        if department is None:
            raise HTTPException(404, "网站不存在")
    if not allowed(site, user):
        raise HTTPException(404, "网站不存在")
    return site


@router.get("/{site_id}/demo/capabilities")
async def demo_capabilities(site_id: str, user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        site = await _demo_site(session, site_id, user)
    policy = load_policy(storage_root(), site_id)
    role = user.permission
    return {"site_id": site_id, "title": site.title, "role": role,
            "can_manage": site.owner_uid == user.uid,
            "components": sorted(key for key in COMPONENTS if granted(policy, "components", key, role)),
            "data": sorted(key for key in DATASETS if granted(policy, "data", key, role))}


@router.get("/{site_id}/demo/data/{dataset}")
async def demo_data(site_id: str, dataset: str, user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        await _demo_site(session, site_id, user)
    if dataset not in DATASETS or not granted(load_policy(storage_root(), site_id), "data", dataset, user.permission):
        raise HTTPException(404, "数据不存在")
    return EXAMPLE_DATA[dataset]


@router.get("/{site_id}/demo/policy")
async def demo_policy(site_id: str, user: UserInfo = Depends(site_login)):
    maker = await init_async_session()
    async with maker() as session:
        _owned(await session.get(PublishedSite, site_id), user)
    return {"roles": sorted(ROLES), "policy": load_policy(storage_root(), site_id)}


@router.put("/{site_id}/demo/policy")
async def update_demo_policy(site_id: str, body: dict,
                             user: UserInfo = Depends(site_login)):
    policy = validate_policy(body)
    maker = await init_async_session()
    async with maker() as session:
        _owned(await session.get(PublishedSite, site_id), user)
    save_policy(storage_root(), site_id, policy)
    logger.info("网站示例角色权限已更新: id={} owner={}", site_id, user.uid)
    return {"roles": sorted(ROLES), "policy": policy}
