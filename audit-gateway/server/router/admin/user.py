"""用户管理路由 - 示例接口（登录 / 用户信息 / Token刷新）"""

from typing import Optional, Union

from config import get_settings
from db.selects.user import get_user_by_username, get_user_info
from db.updates.user import update_user_last_login
from fastapi import APIRouter, Depends, Request
from fastapi import Response as FastAPIResponse
from loguru import logger
from model.request.user import LogoutTokens, RefreshToken, UserLogin
from model.response import LoginInfo, Response, UserInfo
from utils.cache import CacheManager
from utils.crypt import JWTAuth, check_password_async
from utils.dependencies import get_current_user, to_user_info
from utils.token_manager import get_token_manager

router = APIRouter(prefix="/user", tags=["用户管理"])
jwt_auth = JWTAuth()
token_manager = get_token_manager()
config = get_settings()


async def mint_user_tokens(user, *, password_login=False) -> tuple[str, str]:
    """密码与企业身份登录共用同一会话签发和撤销链路。"""
    if password_login and getattr(user, "must_change_password", False):
        access_token = jwt_auth.create_access_token(user, password_change_required=True)
        refresh_token = jwt_auth.create_refresh_token(user, password_change_required=True)
    else:
        access_token = jwt_auth.create_access_token(user)
        refresh_token = jwt_auth.create_refresh_token(user)
    await token_manager.store_token(access_token, str(user.id))
    await token_manager.store_token(refresh_token, str(user.id))
    await update_user_last_login(user.uid)
    return access_token, refresh_token


def _set_auth_cookies(
    response: FastAPIResponse,
    access_token: str,
    refresh_token: str,
    settings=None,
) -> None:
    """写入与 JWT 有效期一致的同源认证 Cookie。"""
    active_settings = settings or config
    common = {
        "httponly": True,
        "secure": active_settings.is_prod,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        "access_token",
        access_token,
        max_age=int(active_settings.access_token_expire_minutes * 60),
        **common,
    )
    response.set_cookie(
        "refresh_token",
        refresh_token,
        max_age=int(active_settings.refresh_token_expire_minutes * 60),
        **common,
    )


# ===================== 示例接口 1: 用户登录 =====================

@router.post("/login", response_model=Response)
async def user_login(request: UserLogin, req: Request):
    """用户登录

    - 按用户名查账号（用户名是中文花名）
    - 校验密码、账号状态
    - 生成 access_token / refresh_token 并存入 Redis 白名单

    失败日志把用户名与密码长度用『』框起来，方便一眼看出有没有混进空格、
    引号或不可见字符 —— 这类问题在中文用户名 + 复杂密码的场景很常见。
    """
    username = (request.username or "").strip()
    client_ip = req.client.host if req.client else ""

    user = await get_user_by_username(username)
    if user is None:
        logger.warning(
            f"登录失败（用户名不存在）：用户名『{username}』"
            f"长度={len(username)} 来源={client_ip}"
        )
        return Response(code=10001)

    if not await check_password_async(request.password, user.password):
        logger.warning(
            f"登录失败（密码不匹配）：用户名『{user.username}』"
            f"密码长度={len(request.password)} 来源={client_ip}"
        )
        return Response(code=10001)

    if user.status != 1:
        logger.warning(f"登录被拒（账号已禁用）：用户名『{user.username}』")
        return Response(code=10007)

    access_token, refresh_token = await mint_user_tokens(user, password_login=True)
    logger.info(
        f"登录成功：用户名『{user.username}』role={user.permission} "
        f"dept={user.dept_id} uid={user.uid}"
    )

    login_info = LoginInfo(
        access_token=access_token,
        refresh_token=refresh_token,
        user=await to_user_info(user, password_change_required=getattr(user, "must_change_password", False)),
    )

    resp = FastAPIResponse(
        content=Response(
            data=login_info.model_dump(),
        ).model_dump_json(),
        media_type="application/json",
    )
    _set_auth_cookies(resp, access_token, refresh_token)
    return resp


# ===================== 示例接口 2: 获取当前用户信息 =====================

@router.get("/info", response_model=Response)
async def user_info(user: Union[UserInfo, Response] = Depends(get_current_user)):
    """
    示例接口 ② - 获取当前登录用户信息

    - 通过 Cookie / Authorization 头携带 token
    - 依赖项 get_current_user 自动完成验证
    """
    if isinstance(user, Response):
        return user
    return Response(data=user.model_dump())


# ===================== 示例接口 3: 刷新 Token =====================

@router.post("/refresh", response_model=Response)
async def refresh_token(request: RefreshToken, req: Request):
    """
    刷新访问令牌

    refresh_token 取值优先级：请求体 > httpOnly Cookie。
    后台前端刷新页面后内存里的令牌就没了，只剩 Cookie，因此必须支持 Cookie 回退。
    """
    from jose import ExpiredSignatureError, JWTError, jwt

    token = request.refresh_token or req.cookies.get("refresh_token")
    if not token:
        return Response(code=10003)

    try:
        payload = jwt.decode(
            token, config.jwt_refresh_secret_key, config.algorithm
        )
        uid = payload.get("sub")
        if uid is None:
            return Response(code=10003)
    except (JWTError, ExpiredSignatureError):
        return Response(code=10003)

    # 检查 refresh_token 是否被撤销。例外：它是被一次刷新轮换作废的、而那次签发的
    # 继任者从没被用过 —— 说明客户端没收到那次回包，允许再换一次（见 token_manager）。
    replayed_rotation = None
    if await token_manager.is_token_revoked(token):
        record = await token_manager.get_revocation(token)
        if not await token_manager.rotation_successor_unused(record):
            return Response(code=10003)
        replayed_rotation = record
        logger.warning(
            f"刷新回包疑似丢失，允许旧 refresh_token 重换一次: uid={uid} "
            f"successor={record.get('successor_refresh_jti')}"
        )

    user = await get_user_info(uid)
    if user is None:
        return Response(code=10003)

    if user.status != 1:
        return Response(code=10007)

    # 生成新的 token
    restricted = bool(payload.get("password_change_required") and getattr(user, "must_change_password", False))
    auth_time = payload.get("auth_time")
    new_access = jwt_auth.create_access_token(user, password_change_required=restricted, auth_time=auth_time)
    new_refresh = jwt_auth.create_refresh_token(user, password_change_required=restricted, auth_time=auth_time)

    if replayed_rotation:
        # 上一次签发、客户端没收到的那对，作废掉，别留一对没人持有的活令牌
        for kind in ("access", "refresh"):
            await token_manager.revoke_jti(
                replayed_rotation.get(f"successor_{kind}_jti"),
                uid,
                int(replayed_rotation.get(f"successor_{kind}_exp") or 0),
                reason="refresh_replaced",
            )

    # 撤销旧的 refresh_token（一次性使用，防止重放），并记下继任者供宽限判断
    successors = {}
    for kind, issued in (("access", new_access), ("refresh", new_refresh)):
        identity = token_manager.token_identity(issued) or {}
        successors[f"successor_{kind}_jti"] = identity.get("jti")
        successors[f"successor_{kind}_exp"] = identity.get("exp")
    await token_manager.revoke_token(token, reason="refresh", extra=successors)

    # 存储新 token
    await token_manager.store_token(new_access, str(user.id))
    await token_manager.store_token(new_refresh, str(user.id))

    login_info = LoginInfo(
        access_token=new_access,
        refresh_token=new_refresh,
        user=await to_user_info(user, password_change_required=restricted),
    )

    resp = FastAPIResponse(
        content=Response(data=login_info.model_dump()).model_dump_json(),
        media_type="application/json",
    )
    _set_auth_cookies(resp, new_access, new_refresh)
    return resp


# ===================== 接口 4: 登出 =====================

@router.post("/logout", response_model=Response)
async def user_logout(req: Request, body: Optional[LogoutTokens] = None):
    """登出

    - 撤销当前 access_token / refresh_token（写 Redis 黑名单）
    - 清除 Cookie
    - 清掉用户信息缓存，避免改角色后旧缓存继续生效
    """
    access = req.cookies.get("access_token")
    auth_header = req.headers.get("Authorization")
    if not access and auth_header and auth_header.startswith("Bearer "):
        access = auth_header.split(" ")[1]

    refresh = (body.refresh_token if body else None) or req.cookies.get("refresh_token")

    # 工作台退出同时撤销该账号在接入应用的短期访问令牌。
    decode = getattr(token_manager, "_decode_token", lambda _token: None)
    identity = (decode(access) if access else None) or (decode(refresh) if refresh else None)

    if access:
        await token_manager.revoke_token(access, reason="logout")
        async with CacheManager.get_instance().redis_connection() as redis:
            await redis.delete("user:" + access)
    if refresh:
        await token_manager.revoke_token(refresh, reason="logout")
    if identity and identity.get("sub"):
        from services.enterprise_sso import revoke_user_sessions
        try:
            await revoke_user_sessions(identity["sub"])
        except Exception as exc:
            # Redis 故障时 UserInfo 会拒绝访问；不因此让工作台退出失败。
            logger.error("撤销统一登录应用会话失败: uid={} error={}", identity["sub"], exc)

    resp = FastAPIResponse(
        content=Response(message="已退出登录").model_dump_json(),
        media_type="application/json",
    )
    resp.delete_cookie("access_token")
    resp.delete_cookie("refresh_token")
    return resp
