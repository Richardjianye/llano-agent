"""FastAPI 依赖项 - 用户认证与信息获取"""

import time
from typing import Optional, Union

from config import get_settings
from db.selects.user import get_user_info
from fastapi import Depends, Request
from jose import ExpiredSignatureError, JWTError, jwt
from loguru import logger
from model.database import ROLE_PRESIDENT
from model.response import Response, UserInfo
from services.model_access import AccessSubject
from services.permissions import has_permission, permissions_of
from utils.cache import CacheManager
from utils.errors import AppError, PermissionError_
from utils.token_manager import get_token_manager

config = get_settings()
cache = CacheManager.get_instance()
token_manager = get_token_manager()


async def check_token(token: str, *, allow_password_change: bool = False):
    """验证token并获取用户信息"""
    if token is None:
        return Response(code=10002, message="未提供认证令牌，请重新登录")

    try:
        payload = jwt.decode(token, config.jwt_secret_key, config.algorithm)
        uid: str = payload.get("sub")
        exp: int = payload.get("exp")
        if uid is None:
            return Response(code=10002, message="无效的令牌，请重新登录")
    except (JWTError, ExpiredSignatureError) as e:
        logger.warning(f"Token验证失败: {e}")
        return Response(code=10002, message="令牌无效或已过期，请重新登录")

    user = await get_user_info(uid)
    if user is None:
        return Response(code=10002, message="用户不存在")

    if user.status != 1:
        return Response(code=10007, message="账号已停用，如有疑问请联系管理员")

    restricted = bool(payload.get("password_change_required") and getattr(user, "must_change_password", False))
    if restricted and not allow_password_change:
        return Response(code=10012, message="请先修改初始密码")
    return await to_user_info(user, exp, password_change_required=restricted)


async def to_user_info(user, exp: Optional[int] = None, *, password_change_required=False) -> UserInfo:
    """User ORM -> UserInfo

    顺带补上部门名与部门编码：前者用于展示，后者是审计可见性的判定依据
    （决策 #8：会话内容仅管理部门可见）。
    """
    dept_name = None
    dept_code = None
    if user.dept_id is not None:
        from db.selects.department import get_department

        dept = await get_department(user.dept_id)
        if dept:
            dept_name = dept.name
            dept_code = dept.code

    return UserInfo(
        id=user.id,
        uid=user.uid,
        username=user.username,
        realname=user.realname,
        nickname=user.nickname,
        permission=user.permission,
        dept_id=user.dept_id,
        dept_name=dept_name,
        dept_code=dept_code,
        status=user.status,
        expired=exp,
        permissions=list(permissions_of(user.permission, dept_code)),
        default_route="dashboard",
        must_change_password=password_change_required,
    )


async def get_token_from_cookie(request: Request) -> Optional[str]:
    """从Cookie中获取access_token"""
    return request.cookies.get("access_token")


async def get_current_user(
    token: Optional[str] = Depends(get_token_from_cookie),
    request: Request = None,
):
    """
    从Cookie中获取token并验证，返回当前用户信息
    支持从Cookie和Authorization头两种方式获取token
    """
    if not token and request:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        return Response(code=10002, message="登录态已过期（未提供认证令牌），请重新登录")

    # 黑名单检查
    if await token_manager.is_token_revoked(token):
        logger.warning(f"尝试使用已撤销的token: {token[:20]}...")
        return Response(code=10002, message="登录态已过期（令牌失效），请重新登录")

    # 初始密码会话每次查库，不能通过旧缓存绕过改密或账号状态校验。
    try:
        payload = jwt.decode(token, config.jwt_secret_key, config.algorithm)
    except (JWTError, ExpiredSignatureError):
        return Response(code=10002)
    if payload.get("password_change_required"):
        allowed = bool(request and request.url.path in {
            "/admin/user/info", "/admin/account/me/password",
        })
        return await check_token(token, allow_password_change=allowed)

    # 账号状态与角色由数据库即时判定，避免同步禁用后被旧用户缓存继续放行。
    return await check_token(token)


# ==================== RBAC ====================


async def require_login(
    user: Union[UserInfo, Response] = Depends(get_current_user)
) -> UserInfo:
    """要求已登录。失败直接抛 AppError 中断请求（不同于 get_current_user 返回 Response）"""
    if isinstance(user, Response):
        raise AppError(code=user.code, message=user.message)
    return user


def require_permission(permission: str):
    """功能权限依赖项工厂 —— 权限点定义见 services/permissions.py

        @router.post("/create")
        async def create(user: UserInfo = Depends(require_permission("dept:edit"))):

    这里**没有超级角色兜底**：总裁与开发者能过，是因为权限表里写了它们，
    而不是因为某个角色被硬编码成万能。
    """

    async def _checker(user: UserInfo = Depends(require_login)) -> UserInfo:
        # 传部门编码：提示词策略的只读/编辑范围依赖部门
        if not has_permission(user.permission, permission, user.dept_code):
            logger.warning(
                f"越权访问被拒绝: uid={user.uid} role={user.permission} "
                f"dept={user.dept_code} 需要权限 {permission}"
            )
            raise PermissionError_(
                f"当前角色（{user.permission}）无权执行该操作"
            )
        return user

    return _checker


def require_roles(*roles: str):
    """角色白名单依赖项工厂 —— 仅用于没法归到权限点的特例

    一般情况优先用 require_permission；这个不做任何超级角色兜底。
    """
    allowed = set(roles)

    async def _checker(user: UserInfo = Depends(require_login)) -> UserInfo:
        if user.permission not in allowed:
            logger.warning(
                f"越权访问被拒绝: uid={user.uid} role={user.permission} 需要 {sorted(allowed)}"
            )
            raise PermissionError_(
                f"当前角色（{user.permission}）无权执行该操作"
            )
        return user

    return _checker


async def require_president(user: UserInfo = Depends(require_login)) -> UserInfo:
    """仅总裁可执行的操作"""
    if user.permission != ROLE_PRESIDENT:
        raise PermissionError_("该操作仅总裁可执行")
    return user


async def get_access_subject(user: UserInfo = Depends(require_login)) -> AccessSubject:
    """构造模型白名单判定主体"""
    return AccessSubject(uid=user.uid, role=user.permission, dept_id=user.dept_id)


def invalidate_user_cache_key(token: str) -> str:
    """用户信息缓存 key（改角色/部门后需要主动清理，否则最长 1 小时才生效）"""
    return "user:" + token
