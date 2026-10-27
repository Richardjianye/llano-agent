"""企业应用统一登录：OIDC Authorization Code + PKCE。"""

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jose import jwt
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from config import get_settings
from db import init_async_session
from db.selects.user import get_user_info
from model.database import SSOClient
from model.response import Response as GatewayResponse, UserInfo
from services.enterprise_sso import (SSOError, access_record, check_client, check_redirect,
                                     check_scope, get_client, issue_code, issue_tokens, issuer_settings,
                                     jwks as published_jwks, redeem_code, revoke_access, valid_redirect)
from utils.dependencies import get_current_user, require_permission, to_user_info

router = APIRouter(tags=["企业统一登录"])
admin_router = APIRouter(prefix="/admin/sso", tags=["统一登录应用管理"])
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


@router.get("/.well-known/openid-configuration")
async def discovery():
    _, issuer, _, _ = issuer_settings()
    return {"issuer": issuer, "authorization_endpoint": issuer + "/sso/authorize",
            "token_endpoint": issuer + "/sso/token", "userinfo_endpoint": issuer + "/sso/userinfo",
            "jwks_uri": issuer + "/sso/jwks", "revocation_endpoint": issuer + "/sso/revoke",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
            "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": ["openid", "profile", "org"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"]}


@router.get("/sso/jwks")
async def jwks():
    return JSONResponse({"keys": published_jwks()}, headers={"Cache-Control": "public, max-age=60"})


def _authorization_error(redirect_uri: str, state: str, error: SSOError) -> RedirectResponse:
    """回调地址已核验后，按 OIDC Core 3.1.2.6 把错误带回接入方，而不是停在网关页面。"""
    params = {"error": error.code, "error_description": error.message}
    if state:
        params["state"] = state
    separator = "&" if urlparse(redirect_uri).query else "?"
    response = RedirectResponse(redirect_uri + separator + urlencode(params), status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/sso/authorize")
async def authorize(request: Request, client_id: str = "", redirect_uri: str = "",
                    response_type: str = "", scope: str = "", state: str = "", nonce: str = "",
                    code_challenge: str = "", code_challenge_method: str = "", prompt: str = ""):
    issuer_settings()
    # 应用和回调地址未核验前不能回跳，否则就是开放重定向；这两类错误直接返回给浏览器。
    client = await get_client(client_id)
    check_redirect(client, redirect_uri)
    try:
        if response_type != "code":
            raise SSOError("仅支持 response_type=code", 400, "unsupported_response_type")
        check_scope(client, scope)
        if (code_challenge_method != "S256" or not _CHALLENGE.fullmatch(code_challenge)
                or not 24 <= len(state) <= 256 or not 24 <= len(nonce) <= 256):
            raise SSOError("授权参数不符合 OIDC 授权码及 PKCE 要求")
        prompts = set(prompt.split())
        if "none" in prompts and len(prompts) > 1:
            raise SSOError("prompt=none 不能与其他取值同时使用")

        current = await get_current_user(token=request.cookies.get("access_token"), request=request)
        if isinstance(current, GatewayResponse):
            if "none" in prompts:
                raise SSOError("需要先登录工作台", 400, "login_required")
            continuation = request.url.path + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse("/login?redirect=" + quote(continuation, safe=""), status_code=303)
        user = await get_user_info(uid=current.uid)
        if user is None or user.status != 1 or user.account_kind != "internal":
            raise SSOError("账号不可用", 403, "access_denied")
        access = request.cookies.get("access_token")
        auth_time = int(time.time())
        session_jti = None
        if access:
            claims = jwt.get_unverified_claims(access)
            auth_time = int(claims.get("auth_time") or auth_time)
            session_jti = claims.get("jti")
        if not session_jti:
            raise SSOError("当前登录会话无效", 401, "access_denied")
        code = await issue_code(client=client, redirect_uri=redirect_uri, scope=scope, nonce=nonce,
                                challenge=code_challenge, uid=user.uid, auth_time=auth_time,
                                session_jti=session_jti)
    except SSOError as exc:
        if exc.code == "temporarily_unavailable":
            raise
        return _authorization_error(redirect_uri, state[:256], exc)
    separator = "&" if urlparse(redirect_uri).query else "?"
    result = RedirectResponse(redirect_uri + separator + urlencode({"code": code, "state": state}),
                              status_code=303)
    result.headers["Cache-Control"] = "no-store"
    return result


def _client_credentials(client_id: str, client_secret: str | None, authorization: str | None):
    if authorization:
        try:
            scheme, encoded = authorization.split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError
            basic_id, basic_secret = base64.b64decode(encoded, validate=True).decode().split(":", 1)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SSOError("客户端认证格式无效", 401, "invalid_client") from exc
        if client_id and basic_id != client_id:
            raise SSOError("客户端身份不一致", 401, "invalid_client")
        return basic_id, basic_secret
    return client_id, client_secret


async def _authenticated_client(client_id: str, client_secret: str | None, authorization: str | None):
    resolved_id, secret = _client_credentials(client_id, client_secret, authorization)
    try:
        client = await get_client(resolved_id)
    except SSOError as exc:
        # RFC 6749 §5.2：令牌端点上的客户端认证失败统一 401，不区分未登记与已停用。
        raise SSOError("客户端认证失败", 401, "invalid_client") from exc
    if client.client_type == "confidential":
        digest = hashlib.sha256((secret or "").encode()).hexdigest()
        if not client.secret_hash or not secrets.compare_digest(digest, client.secret_hash):
            raise SSOError("客户端认证失败", 401, "invalid_client")
    elif client.client_type != "public" or secret:
        raise SSOError("客户端认证失败", 401, "invalid_client")
    return client


@router.post("/sso/token")
async def token(grant_type: str = Form(), code: str = Form(), redirect_uri: str = Form(),
                code_verifier: str = Form(), client_id: str = Form(default=""),
                client_secret: str | None = Form(default=None), authorization: str | None = Header(default=None)):
    issuer_settings()
    if grant_type != "authorization_code" or not _VERIFIER.fullmatch(code_verifier):
        raise SSOError("不支持的授权类型或 PKCE verifier", 400, "invalid_grant")
    client = await _authenticated_client(client_id, client_secret, authorization)
    check_client(client, redirect_uri, "openid")
    data = await redeem_code(code, code_verifier, client, redirect_uri)
    user = await get_user_info(uid=data["uid"])
    if user is None or user.status != 1 or user.account_kind != "internal":
        raise SSOError("账号不可用", 403, "access_denied")
    info = await to_user_info(user)
    result = JSONResponse(await issue_tokens(data, user, info.dept_code))
    result.headers["Cache-Control"] = "no-store"
    result.headers["Pragma"] = "no-cache"
    return result


@router.get("/sso/userinfo")
async def userinfo(authorization: str | None = Header(default=None)):
    issuer_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise SSOError("缺少应用访问令牌", 401, "invalid_token")
    record = await access_record(authorization[7:])
    try:
        await get_client(record["client_id"])
    except SSOError as exc:
        raise SSOError("接入应用已停用", 401, "invalid_token") from exc
    user = await get_user_info(uid=record["uid"])
    if user is None or user.status != 1 or user.account_kind != "internal":
        raise SSOError("账号不可用", 401, "invalid_token")
    cfg, _, _, _ = issuer_settings()
    info = await to_user_info(user)
    result = {"sub": user.uid, "account_status": "active"}
    if "org" in record["scope"].split():
        result.update({"org_id": cfg.sso_org_id, "dept_code": info.dept_code})
    if "profile" in record["scope"].split():
        result.update({"name": user.realname or user.nickname or user.username,
                       "preferred_username": user.username})
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/sso/revoke")
async def revoke(token: str = Form(), client_id: str = Form(default=""),
                 client_secret: str | None = Form(default=None), authorization: str | None = Header(default=None)):
    client = await _authenticated_client(client_id, client_secret, authorization)
    try:
        record = await access_record(token)
    except SSOError as exc:
        if exc.code != "invalid_token":
            raise
        record = None
    if record and record["client_id"] == client.client_id:
        await revoke_access(token)
    return JSONResponse({}, headers={"Cache-Control": "no-store"})


class ClientRegistration(BaseModel):
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,96}$")
    name: str = Field(min_length=1, max_length=128)
    owner_uid: str = Field(min_length=1, max_length=36)
    environment: str = Field(pattern=r"^(dev|prod)$")
    audit_contact: str = Field(min_length=1, max_length=128)
    client_type: str = Field(pattern=r"^(public|confidential)$")
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    allowed_scopes: list[str] = Field(default=["openid", "profile", "org"])


class ClientUpdate(BaseModel):
    """可调整的登记项；client_id、环境与客户端类型变化等同新应用，须重新登记。"""

    name: str = Field(min_length=1, max_length=128)
    owner_uid: str = Field(min_length=1, max_length=36)
    audit_contact: str = Field(min_length=1, max_length=128)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    allowed_scopes: list[str] = Field(min_length=1)


async def _validate_registration(owner_uid: str, redirect_uris: list[str], allowed_scopes: list[str]) -> None:
    cfg = get_settings()
    if len(redirect_uris) != len(set(redirect_uris)):
        raise SSOError("回调地址有重复")
    invalid = [uri for uri in redirect_uris if not valid_redirect(uri, production=cfg.is_prod)]
    if invalid:
        rule = "正式环境只接受 HTTPS" if cfg.is_prod else "须为 HTTPS，或 http://localhost / http://127.0.0.1"
        raise SSOError(f"回调地址无效（{rule}，且不能含 # 片段）：{invalid[0]}")
    if ("openid" not in allowed_scopes or len(allowed_scopes) != len(set(allowed_scopes))
            or not set(allowed_scopes) <= {"openid", "profile", "org", "directory.read"}):
        raise SSOError("scope 必须包含 openid，且只能取 openid / profile / org / directory.read")
    owner = await get_user_info(uid=owner_uid)
    if owner is None or owner.status != 1:
        raise SSOError("应用负责人账号不可用")


@admin_router.post("/clients")
async def register_client(body: ClientRegistration, operator: UserInfo = Depends(require_permission("sso:manage"))):
    current_env = "prod" if get_settings().is_prod else "dev"
    if body.environment != current_env:
        raise SSOError(f"当前网关是 {current_env} 环境，只能登记 {current_env} 应用")
    await _validate_registration(body.owner_uid, body.redirect_uris, body.allowed_scopes)
    secret = secrets.token_urlsafe(48) if body.client_type == "confidential" else None
    maker = await init_async_session()
    async with maker() as session:
        session.add(SSOClient(client_id=body.client_id, name=body.name, owner_uid=body.owner_uid,
                              environment=body.environment, audit_contact=body.audit_contact,
                              client_type=body.client_type, redirect_uris=json.dumps(body.redirect_uris),
                              allowed_scopes=" ".join(body.allowed_scopes),
                              secret_hash=hashlib.sha256(secret.encode()).hexdigest() if secret else None,
                              enabled=True))
        try:
            await session.commit()
        except IntegrityError as exc:
            raise SSOError("应用 ID 已存在", 409) from exc
    logger.warning("统一登录应用已登记: client_id={} owner={} by={}", body.client_id, body.owner_uid, operator.uid)
    return {"client_id": body.client_id, "client_secret": secret,
            "message": "客户端密钥仅此次显示；请保存到接入方服务端密钥存储"}


@admin_router.get("/clients")
async def list_clients(operator: UserInfo = Depends(require_permission("user:view"))):
    from sqlalchemy import select

    maker = await init_async_session()
    async with maker() as session:
        clients = (await session.execute(select(SSOClient).order_by(SSOClient.client_id))).scalars().all()
    return [{"client_id": item.client_id, "name": item.name, "owner_uid": item.owner_uid,
             "environment": item.environment, "audit_contact": item.audit_contact,
             "client_type": item.client_type, "redirect_uris": json.loads(item.redirect_uris),
             "allowed_scopes": item.allowed_scopes.split(), "enabled": item.enabled}
            for item in clients]


@admin_router.post("/clients/{client_id}/disable")
async def disable_client(client_id: str, operator: UserInfo = Depends(require_permission("sso:manage"))):
    maker = await init_async_session()
    async with maker() as session:
        client = await session.get(SSOClient, client_id)
        if client is None:
            raise SSOError("应用不存在", 404)
        client.enabled = False
        await session.commit()
    logger.warning("统一登录应用已停用: client_id={} by={}", client_id, operator.uid)
    return {"client_id": client_id, "enabled": False}


@admin_router.post("/clients/{client_id}/rotate-secret")
async def rotate_client_secret(client_id: str, operator: UserInfo = Depends(require_permission("sso:manage"))):
    maker = await init_async_session()
    secret = secrets.token_urlsafe(48)
    async with maker() as session:
        client = await session.get(SSOClient, client_id)
        if client is None or not client.enabled or client.client_type != "confidential":
            raise SSOError("机密应用不存在或已停用", 404)
        client.secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        await session.commit()
    logger.warning("统一登录应用密钥已轮换: client_id={} by={}", client_id, operator.uid)
    return {"client_id": client_id, "client_secret": secret,
            "message": "旧密钥立即失效；新密钥仅此次显示"}


@admin_router.put("/clients/{client_id}")
async def update_client(client_id: str, body: ClientUpdate, operator: UserInfo = Depends(require_permission("sso:manage"))):
    await _validate_registration(body.owner_uid, body.redirect_uris, body.allowed_scopes)
    maker = await init_async_session()
    async with maker() as session:
        client = await session.get(SSOClient, client_id)
        if client is None:
            raise SSOError("应用不存在", 404)
        before = {"redirect_uris": json.loads(client.redirect_uris), "allowed_scopes": client.allowed_scopes.split(),
                  "owner_uid": client.owner_uid}
        client.name = body.name
        client.owner_uid = body.owner_uid
        client.audit_contact = body.audit_contact
        client.redirect_uris = json.dumps(body.redirect_uris)
        client.allowed_scopes = " ".join(body.allowed_scopes)
        await session.commit()
    # 已签发的授权码在兑换时会按新回调重新核验；已签发访问令牌最长 5 分钟自然过期。
    logger.warning("统一登录应用已修改: client_id={} before={} after={} by={}", client_id, before,
                   {"redirect_uris": body.redirect_uris, "allowed_scopes": body.allowed_scopes,
                    "owner_uid": body.owner_uid}, operator.uid)
    return {"client_id": client_id}


@admin_router.post("/clients/{client_id}/enable")
async def enable_client(client_id: str, operator: UserInfo = Depends(require_permission("sso:manage"))):
    maker = await init_async_session()
    async with maker() as session:
        client = await session.get(SSOClient, client_id)
        if client is None:
            raise SSOError("应用不存在", 404)
        client.enabled = True
        await session.commit()
    logger.warning("统一登录应用已重新启用: client_id={} by={}", client_id, operator.uid)
    return {"client_id": client_id, "enabled": True}
