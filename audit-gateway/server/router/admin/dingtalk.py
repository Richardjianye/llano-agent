"""钉钉网页授权登录与存量账号的一次性显式绑定。"""

import secrets
import json
from urllib.parse import urlencode

from config import get_settings
from db.selects.user import get_user_info
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from model.response import Response, UserInfo
from router.admin.user import _set_auth_cookies, mint_user_tokens
from services.dingtalk_identity import (DingTalkAuthError, authorization_url,
                                         configured, resolve_identity)
from services.external_identity import (IdentityConflictError, bind_identity,
                                        find_identity, find_user_identity)
from utils.cache import CacheManager
from utils.dependencies import get_current_user, require_login

router = APIRouter(prefix="/user/dingtalk", tags=["钉钉登录"])
_STATE_COOKIE = "dingtalk_oauth_state"
_STATE_TTL = 300


@router.get("/config")
async def dingtalk_config():
    enabled = configured()
    return Response(data={"enabled": enabled, "auth_flows": ["native_pkce"] if enabled else []})


@router.get("/status")
async def dingtalk_status(user: UserInfo = Depends(require_login)):
    cfg = get_settings()
    bound = False
    if configured(cfg):
        bound = await find_user_identity("dingtalk", cfg.dingtalk_corp_id, user.uid) is not None
    return Response(data={"enabled": configured(cfg), "bound": bound})


async def _start(mode: str, user_uid: str = "") -> RedirectResponse:
    if not configured():
        raise HTTPException(status_code=503, detail="钉钉登录尚未配置")
    state = secrets.token_urlsafe(32)
    value = mode if mode == "login" or mode.startswith(("native:", "login:")) else "bind:" + user_uid
    async with CacheManager.get_instance().redis_connection() as redis:
        stored = await redis.set("dingtalk:oauth:" + state, value, ex=_STATE_TTL, nx=True)
    if not stored:
        raise HTTPException(status_code=503, detail="无法创建钉钉登录状态")
    response = RedirectResponse(authorization_url(state))
    response.set_cookie(
        _STATE_COOKIE, state, max_age=_STATE_TTL, httponly=True,
        secure=get_settings().is_prod, samesite="lax",
        path="/admin/user/dingtalk/callback",
    )
    return response


@router.get("/login")
async def dingtalk_login(continue_to: str | None = Query(default=None, alias="continue")):
    if continue_to:
        if not continue_to.startswith("/sso/authorize?") or "\\" in continue_to or "\n" in continue_to:
            raise HTTPException(400, "无效的登录后继续地址")
        return await _start("login:" + continue_to)
    return await _start("login")


@router.get("/bind")
async def dingtalk_bind(user: UserInfo = Depends(require_login)):
    return await _start("bind", user.uid)


def _failure(mode: str, reason: str, *, clear_state: bool = True) -> RedirectResponse:
    if mode.startswith("native:"):
        pending = json.loads(mode[7:])
        response = RedirectResponse(pending["redirect_uri"] + "?" + urlencode({"error": reason, "state": pending["state"]}))
    elif mode.startswith("bind:"):
        response = RedirectResponse("/?dingtalk_error=" + reason)
    elif mode.startswith("login:"):
        response = RedirectResponse("/login?" + urlencode({"redirect": mode[6:], "dingtalk_error": reason}))
    else:
        response = RedirectResponse("/login?dingtalk_error=" + reason)
    if clear_state:
        response.delete_cookie(_STATE_COOKIE, path="/admin/user/dingtalk/callback")
    return response


@router.get("/callback")
async def dingtalk_callback(request: Request):
    state = request.query_params.get("state", "")
    cookie_state = request.cookies.get(_STATE_COOKIE, "")
    # 同时绑定浏览器与一次性 Redis 状态；回调重放和登录 CSRF 都会被拒绝。
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return _failure("login", "state", clear_state=False)

    async with CacheManager.get_instance().redis_connection() as redis:
        raw = await redis.getdel("dingtalk:oauth:" + state)
    mode = raw.decode() if isinstance(raw, bytes) else (raw or "")
    if mode != "login" and not mode.startswith(("bind:", "native:", "login:")):
        return _failure("login", "state")

    try:
        if request.query_params.get("error"):
            raise DingTalkAuthError("钉钉用户未完成授权")
        code = request.query_params.get("authCode") or request.query_params.get("code")
        identity = await resolve_identity(code or "")

        if mode.startswith("bind:"):
            current = await get_current_user(
                token=request.cookies.get("access_token"), request=request,
            )
            if not isinstance(current, UserInfo) or current.uid != mode[5:]:
                return _failure(mode, "session")
            user = await get_user_info(uid=current.uid)
            if user is None or user.status != 1 or getattr(user, "account_kind", "internal") != "internal":
                return _failure(mode, "session")
            await bind_identity(
                "dingtalk", identity.corp_id, identity.union_id,
                current.uid, identity.user_id,
            )
            response = RedirectResponse("/?dingtalk=bound")
            logger.info("钉钉身份已绑定: uid={}", current.uid)
        else:
            binding = await find_identity(
                "dingtalk", identity.corp_id, identity.union_id,
            )
            if binding is None:
                return _failure(mode, "unbound")
            user = await get_user_info(uid=binding.user_uid)
            if user is None or user.status != 1 or getattr(user, "account_kind", "internal") != "internal":
                return _failure(mode, "disabled")
            if mode.startswith("native:"):
                from router.admin.native_login import handoff
                response = RedirectResponse(await handoff(mode, user.uid))
            else:
                access, refresh = await mint_user_tokens(user)
                response = RedirectResponse(mode[6:] if mode.startswith("login:") else "/")
                _set_auth_cookies(response, access, refresh)
            logger.info("钉钉登录成功: uid={}", user.uid)
    except IdentityConflictError:
        return _failure(mode, "conflict")
    except DingTalkAuthError as exc:
        logger.warning("钉钉登录失败: {}", exc)
        return _failure(mode, "provider")
    response.delete_cookie(_STATE_COOKIE, path="/admin/user/dingtalk/callback")
    return response
