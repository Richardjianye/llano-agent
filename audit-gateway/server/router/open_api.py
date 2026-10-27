"""对外开放接口 —— 第三方平台（OSS 等）用应用凭据调用，与 /admin 完全分开

    POST /open/v1/token            client_credentials 换 10 分钟访问令牌
    GET  /open/v1/members/search   搜索成员（uid + 昵称 + 状态）

契约：server/MEMBER_SEARCH_API.md 第 2 节。逻辑在 services/open_directory.py。
错误一律按 OAuth 2.0 格式：{"error": "...", "error_description": "..."}。
"""

import base64
import time
from typing import Optional

from fastapi import APIRouter, Form, Header, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger

from config import get_settings
from services import open_directory as od
from utils.cache import get_redis_context

router = APIRouter(prefix="/open/v1", tags=["对外开放接口"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(exc: od.OpenApiError) -> JSONResponse:
    headers = dict(_NO_STORE)
    if exc.status == 401:
        headers["WWW-Authenticate"] = 'Bearer error="%s"' % exc.error
    return JSONResponse({"error": exc.error, "error_description": exc.description},
                        status_code=exc.status, headers=headers)


def _basic_credentials(authorization: Optional[str]) -> tuple:
    if not authorization or not authorization.startswith("Basic "):
        return "", ""
    try:
        decoded = base64.b64decode(authorization[6:].strip()).decode()
    except Exception:  # noqa: BLE001
        return "", ""
    client_id, _, secret = decoded.partition(":")
    return client_id, secret


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else None)


@router.post("/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    scope: str = Form(od.SCOPE_DIRECTORY_READ),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    authorization: Optional[str] = Header(None),
):
    """client_credentials：client_secret_basic 或 client_secret_post"""
    basic_id, basic_secret = _basic_credentials(authorization)
    cid, secret = (basic_id, basic_secret) if basic_id else (client_id, client_secret)
    try:
        if grant_type != "client_credentials":
            raise od.OpenApiError("unsupported_grant_type", "只支持 client_credentials")
        if scope.split() != [od.SCOPE_DIRECTORY_READ]:
            raise od.OpenApiError("invalid_scope", f"scope 只能是 {od.SCOPE_DIRECTORY_READ}")
        client = od.verify_client(await od.load_client(cid), secret,
                                  environment=od.current_environment())
        async with get_redis_context() as redis:
            await od.check_rate(client.client_id, get_settings().open_api_rate_limit_per_minute,
                                redis, int(time.time() // 60))
            body = await od.issue_token(client, redis)
    except od.OpenApiError as exc:
        await od.record_call(client_id=cid or "-", endpoint="token", status=exc.error[:16],
                             client_ip=_client_ip(request))
        return _error(exc)
    await od.record_call(client_id=client.client_id, endpoint="token", status="ok",
                         client_ip=_client_ip(request))
    return JSONResponse(body, headers=_NO_STORE)


@router.get("/members/search")
async def members_search(
    request: Request,
    keyword: str = Query("", max_length=64),
    uids: str = Query("", description="逗号分隔，最多 100 个"),
    status: str = Query("active", description="active / all"),
    limit: int = Query(20, ge=1, le=od.MAX_PAGE),
    offset: int = Query(0, ge=0),
    authorization: Optional[str] = Header(None),
):
    bearer = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    client_id = "-"
    query = None
    try:
        async with get_redis_context() as redis:
            client_id = await od.resolve_token(bearer, redis)
            await od.check_rate(client_id, get_settings().open_api_rate_limit_per_minute,
                                redis, int(time.time() // 60))
        query = od.OpenMemberQuery(
            keyword=keyword, uids=tuple(uids.split(",")), status=status, limit=limit, offset=offset,
        ).validated()
        page = await od.search_open_members(query)
        await od.record_call(client_id=client_id, endpoint="members.search", status="ok", query=query,
                             result_count=len(page["items"]), client_ip=_client_ip(request), strict=True)
    except od.OpenApiError as exc:
        if exc.error != "temporarily_unavailable":
            await od.record_call(client_id=client_id, endpoint="members.search", status=exc.error[:16],
                                 query=query, client_ip=_client_ip(request))
        return _error(exc)
    logger.info(f"对外成员搜索 client={client_id} {query.summary()} 返回 {len(page['items'])} 条")
    return JSONResponse(page, headers=_NO_STORE)
