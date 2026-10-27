"""账号管理路由 —— 权限点 user:view / user:edit

除功能权限外，所有写操作都要过 **角色级别校验**：只能操作级别严格低于自己的账号。
- 总裁（100）可以停用开发者（90）
- 开发者之间互不能停用，也动不了总裁
- 谁都不能把别人提到与自己同级或更高

任何越权都返回 10008，并写 warning 日志。
"""

from db.inserts.user import insert_user
from db.selects.department import get_department
from db.selects.user import (check_username, count_users, get_user_info,
                             latest_chat_request_at, list_users)
from db.updates.apikey import revoke_user_api_keys
from db.updates.user import (update_user_info, update_user_password,
                             update_user_status)
from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from model.database import ASSIGNABLE_ROLES, User, role_level
from model.request.account import (AccountAssign, AccountCreate,
                                   AccountPasswordReset, AccountStatus,
                                   SelfPasswordChange)
from model.response import Response, UserInfo
from db import init_async_session
from sqlalchemy import update
from services.permissions import (can_assign_role, can_manage,
                                  manageable_roles, permissions_of)
from utils.crypt import check_password_async, hash_password_async
from utils.dependencies import require_login, require_permission
from utils.errors import (ConflictError, NotFoundError, PermissionError_,
                          ValidationError_)
from utils.generate import generate_password, generate_user_id
from utils.token_manager import get_token_manager

router = APIRouter(prefix="/account", tags=["账号管理"])
token_manager = get_token_manager()


async def _load_target(uid: str, operator: UserInfo) -> User:
    """取出目标账号并做级别校验，不通过直接抛 10008"""
    target = await get_user_info(uid=uid)
    if target is None:
        raise NotFoundError("账号不存在")

    if target.uid == operator.uid:
        raise ValidationError_("不能对自己执行该操作")

    if not can_manage(operator.permission, target.permission):
        logger.warning(
            f"越权账号操作被拒绝: operator={operator.uid}({operator.permission}) "
            f"target={target.uid}({target.permission})"
        )
        raise PermissionError_(
            f"你的角色无权管理「{target.realname or target.username}」"
            f"（对方角色级别不低于你）"
        )
    return target


def _account_payload(user: User, operator: UserInfo, last_request_at=None) -> dict:
    return {
        "uid": user.uid,
        "username": user.username,
        "realname": user.realname,
        "nickname": user.nickname,
        "permission": user.permission,
        "dept_id": user.dept_id,
        "status": user.status,
        "account_kind": user.account_kind,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "last_request_at": last_request_at.isoformat() if last_request_at else None,
        # 前端据此决定按钮的显隐，真正的拦截仍在后端
        "manageable": user.uid != operator.uid
        and can_manage(operator.permission, user.permission),
    }


# ==================== 查询 ====================


@router.get("/me/context", response_model=Response)
async def my_context(operator: UserInfo = Depends(require_permission("user:view"))):
    """当前操作者的权限上下文 —— 前端渲染角色下拉框与按钮用"""
    return Response(
        data={
            "role": operator.permission,
            "role_level": role_level(operator.permission),
            "dept_code": operator.dept_code,
            # 审计权限依赖部门，所以这里必须按「角色 + 部门」算
            "permissions": list(
                permissions_of(operator.permission, operator.dept_code)
            ),
            "assignable_roles": list(manageable_roles(operator.permission)),
        }
    )


@router.get("/list", response_model=Response)
async def account_list(
    dept_id: int = None,
    role: str = None,
    keyword: str = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = Query("id"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    operator: UserInfo = Depends(require_permission("user:view")),
):
    """账号列表，支持按部门/角色/用户名筛选"""
    users = await list_users(
        dept_id=dept_id,
        role=role,
        keyword=keyword,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    total = await count_users(dept_id=dept_id, role=role, keyword=keyword)
    activity = await latest_chat_request_at([user.uid for user in users])
    return Response(
        data={
            "items": [
                _account_payload(u, operator, activity.get(u.uid)) for u in users
            ],
            "total": total,
        }
    )


# ==================== 写操作 ====================


@router.post("/create", response_model=Response)
async def account_create(
    body: AccountCreate,
    operator: UserInfo = Depends(require_permission("user:edit")),
):
    """新建账号

    初始密码留空则随机生成，**只在本次响应里返回一次**，之后库里只有哈希。
    """
    if not can_assign_role(operator.permission, body.permission):
        raise PermissionError_("不能创建与自己同级或更高级别的账号")

    if await check_username(body.username):
        raise ConflictError(f"用户名「{body.username}」已存在")

    if body.dept_id is not None and not await get_department(body.dept_id):
        raise NotFoundError("部门不存在")

    plain_password = body.password or generate_password()

    user = User(
        uid=generate_user_id(),
        username=body.username,
        password=await hash_password_async(plain_password),
        nickname=body.nickname or body.realname or body.username,
        realname=body.realname,
        permission=body.permission,
        dept_id=body.dept_id,
        status=body.status,
    )
    await insert_user(user)

    logger.info(
        f"账号已创建: username={body.username} role={body.permission} "
        f"dept={body.dept_id} by={operator.uid}"
    )
    return Response(
        data={
            "uid": user.uid,
            # 生成的密码只回显一次，请当场交给本人
            "initial_password": None if body.password else plain_password,
        }
    )


@router.post("/assign", response_model=Response)
async def account_assign(
    body: AccountAssign,
    operator: UserInfo = Depends(require_permission("user:edit")),
):
    """调整账号的部门 / 角色 / 姓名。

    角色或部门决定提示词策略等敏感后台权限，因此授权上下文变化后立即撤销目标
    账号的全部登录会话。仅修改姓名/昵称不影响权限，不应把对方无故踢下线。
    """
    target = await _load_target(body.uid, operator)

    if body.permission is not None and not can_assign_role(
        operator.permission, body.permission
    ):
        raise PermissionError_("不能把账号提升到与自己同级或更高的角色")

    if body.dept_id is not None and not await get_department(body.dept_id):
        raise NotFoundError("目标部门不存在")

    provided_fields = set(body.model_fields_set)
    authorization_changed = (
        body.permission is not None and body.permission != target.permission
    ) or (
        "dept_id" in provided_fields and body.dept_id != target.dept_id
    )

    update_fields = provided_fields - {"uid"}
    # permission=null 仍表示「不改角色」；角色列不可为空。dept_id=null 则是明确
    # 移出部门，必须保留下来交给 update_user_info 写成 SQL NULL。
    if body.permission is None:
        update_fields.discard("permission")

    await update_user_info(
        User(
            uid=body.uid,
            dept_id=body.dept_id,
            permission=body.permission,
            realname=body.realname,
            nickname=body.nickname,
        ),
        fields=update_fields,
    )
    revoked_sessions = 0
    if authorization_changed:
        revoked_sessions = await token_manager.revoke_user_tokens(
            body.uid, reason="account_assignment_changed"
        )
    logger.info(
        f"账号归属调整: uid={body.uid}({target.username}) dept={body.dept_id} "
        f"role={body.permission} revoked_sessions={revoked_sessions} by={operator.uid}"
    )
    if authorization_changed:
        return Response(message="调整成功；角色或部门已变更，对方需重新登录")
    return Response(message="调整成功")


@router.post("/status", response_model=Response)
async def account_status(
    body: AccountStatus,
    operator: UserInfo = Depends(require_permission("user:edit")),
):
    """启用 / 禁用账号 —— 总裁停用开发者走的就是这个接口"""
    target = await _load_target(body.uid, operator)

    await update_user_status(body.uid, body.status)

    # 重新启用也推进会话撤销水位，防止停用期间 Redis 短暂不可用时旧应用令牌复活。
    await token_manager.revoke_user_tokens(
        body.uid, reason="account_disabled" if body.status == 0 else "account_enabled"
    )
    if body.status == 0:
        await revoke_user_api_keys(body.uid, reason="account_disabled", by=operator.uid)

    action = "启用" if body.status == 1 else "禁用"
    logger.warning(
        f"账号{action}: uid={body.uid}({target.username}/{target.permission}) "
        f"by={operator.uid}({operator.permission})"
    )
    if body.status == 1:
        message = "已启用账号；此前吊销的登录会话和 API Key 不会恢复，需重新登录或签发"
    else:
        message = "已禁用账号；登录会话和 API Key 已吊销，重新启用不会自动恢复"
    return Response(message=message)


@router.post("/reset-password", response_model=Response)
async def account_reset_password(
    body: AccountPasswordReset,
    operator: UserInfo = Depends(require_permission("user:edit")),
):
    """重置密码。留空则随机生成，明文只在本次响应返回一次"""
    target = await _load_target(body.uid, operator)

    plain_password = body.password or generate_password()
    await update_user_password(
        User(uid=body.uid, password=await hash_password_async(plain_password))
    )
    await token_manager.revoke_user_tokens(body.uid, reason="password_reset")
    await revoke_user_api_keys(body.uid, reason="password_reset", by=operator.uid)

    logger.warning(f"账号密码被重置: uid={body.uid}({target.username}) by={operator.uid}")
    return Response(
        data={"initial_password": None if body.password else plain_password},
        message="密码已重置",
    )


@router.post("/me/password", response_model=Response)
async def change_own_password(
    body: SelfPasswordChange,
    request: Request,
    operator: UserInfo = Depends(require_login),
):
    """自助修改密码 —— 任何登录用户都能用，不需要 user:edit

    ## 为什么单独开一个接口，而不是让大家用 /reset-password

    那个接口第一件事就是 `_load_target`，里面写着「不能对自己执行该操作」。
    所以在这之前，**总裁改不了自己的密码** —— 管理接口拒绝自我操作，
    而自助入口根本不存在。首次登录时那句「请立即登录后台修改密码」无处可去。

    ## 为什么必须验当前密码

    验了它，这就是「修改密码」；不验，它是「重置密码」。差别不在名字上：

    不验的话，任何拿到一个有效会话的人都能改掉密码，把机主锁在门外 ——
    一次会话劫持直接升级成永久接管。验了它，攻击者顶多在会话有效期内活动，
    机主改一次密码就把他踢出去。

    重置（不需要旧密码）保留给两条路：管理员对下属走 /reset-password，
    超管自己被锁走服务器上的 scripts/reset_account_password.py。
    那两条都需要「Web 会话之外的东西」—— 一个是更高的角色，一个是服务器权限。

    ## 改完踢掉其它会话

    改密码的常见动机就是「我怀疑有人在用我的号」。不吊销其它会话的话，
    那个人手上的令牌照样有效，改密码只是让机主自己安心了一下。

    当前这条会话不吊销 —— 把操作者自己踢出登录页，只会让人以为改失败了。
    """
    user = await get_user_info(uid=operator.uid)
    if user is None:
        raise NotFoundError("账号不存在")

    client_ip = request.client.host if request.client else "-"

    if not await check_password_async(body.current_password, user.password):
        # 不说「当前密码错误」之外的任何细节，也不区分账号状态 ——
        # 这个接口是登录之后才够得着的，但仍然值得按爆破面对待。
        logger.warning(
            f"自助改密失败（当前密码不符）: uid={operator.uid} 来源={client_ip}"
        )
        raise ValidationError_("当前密码不正确")

    if body.new_password == body.current_password:
        raise ValidationError_("新密码不能与当前密码相同")

    await update_user_password(
        User(uid=operator.uid, password=await hash_password_async(body.new_password))
    )

    factory = await init_async_session()
    async with factory() as session:
        await session.execute(update(User).where(User.uid == operator.uid).values(must_change_password=False))
        await session.commit()

    current_token = _bearer_or_cookie(request)
    revoked = await token_manager.revoke_user_tokens(
        operator.uid, reason="self_password_change"
    )
    if current_token:
        # 重新签发当前这条，避免把操作者自己踢下线
        await token_manager.store_token(current_token, operator.uid)

    if body.revoke_api_keys:
        await revoke_user_api_keys(operator.uid, reason="self_password_change", by=operator.uid)

    logger.warning(
        f"自助改密成功: uid={operator.uid}({user.username}) 来源={client_ip} "
        f"吊销会话={revoked} 吊销ApiKey={body.revoke_api_keys}"
    )
    return Response(message="密码已修改，其它设备上的登录已失效")


def _bearer_or_cookie(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.cookies.get("access_token") or ""


@router.get("/roles", response_model=Response)
async def role_options(operator: UserInfo = Depends(require_permission("user:view"))):
    """全部角色及当前操作者可分配的子集"""
    return Response(
        data={
            "all": [
                {"value": r, "level": role_level(r)} for r in ASSIGNABLE_ROLES
            ],
            "assignable": list(manageable_roles(operator.permission)),
        }
    )
