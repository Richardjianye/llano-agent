"""转发入口的认证 —— 同时接受 JWT 与网关签发的 API Key

两条通道对应两种客户端：
- 管理后台：浏览器，httpOnly Cookie / Authorization: Bearer <JWT>
- 桌面客户端：hermes 直接把网关当 OpenAI 兼容端点用，
  只会发 `Authorization: Bearer sk-llano-xxx`

不管哪条进来，最终都归一成 Caller，后面的白名单判定、审计、用量统计
只认 Caller，不关心它是怎么进来的。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from db.selects.apikey import get_key_by_prefix
from db.selects.department import get_department
from db.selects.user import get_user_info
from db.updates.apikey import touch_api_key
from fastapi import Request
from loguru import logger
from model.database import now
from model.response import Response, UserInfo
from services.apikey_lifecycle import IDLE_MESSAGE, configured_idle_days, is_idle
from services.model_access import AccessSubject
from utils.apikey import key_prefix_of, looks_like_api_key, verify_api_key
from utils.dependencies import check_token
from utils.errors import ApiKeyAuthError, AuthError
from utils.token_manager import get_token_manager


token_manager = get_token_manager()


@dataclass(frozen=True)
class Caller:
    """转发请求的发起人"""

    uid: str
    username: Optional[str]
    role: str
    dept_id: Optional[int]
    dept_code: Optional[str]
    auth_type: str = "jwt"          # jwt | apikey
    api_key_id: Optional[int] = None
    api_key_token_limit: Optional[int] = None

    # 凭据自身携带的会话作用域（阶段 24）。默认 private —— 后台 JWT 与个人设备
    # Key 都是本人在用。群聊会话令牌会把它带成 group，且**不可由请求头放宽**，
    # 判定见 services/conversation_scope.resolve_scope。
    scope: str = "private"
    scope_project_id: Optional[int] = None

    def as_subject(self) -> AccessSubject:
        """转成模型白名单的判定主体"""
        return AccessSubject(uid=self.uid, role=self.role, dept_id=self.dept_id)


def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization")
    if header and header.startswith("Bearer "):
        return header[7:].strip()
    # 后台页面里调转发接口时走 Cookie
    return request.cookies.get("access_token")


async def _caller_from_api_key(plain: str) -> Caller:
    record = await get_key_by_prefix(key_prefix_of(plain))

    # 前缀不存在与哈希不匹配返回同一句话，避免把「这把 Key 存在过」漏出去
    if record is None or not verify_api_key(plain, record.key_hash):
        raise ApiKeyAuthError("API Key 无效")

    if record.status != 1:
        raise ApiKeyAuthError("API Key 已吊销")

    if record.expires_at is not None and record.expires_at < now():
        raise ApiKeyAuthError("API Key 已过期")

    # 闲置失效：只清掉已经没有客户端能续上的 Key，依据与阈值下限见 apikey_lifecycle
    idle_days = configured_idle_days()
    if is_idle(record, now(), idle_days):
        # 碰到就正式吊销，不只是拒绝：台账上立刻能看到，也不必等巡检
        asyncio.create_task(_revoke_idle_quietly(record.id, idle_days))
        raise ApiKeyAuthError(IDLE_MESSAGE)

    user = await get_user_info(uid=record.user_uid)
    if user is None:
        raise ApiKeyAuthError("API Key 对应的账号不存在")

    if user.status != 1:
        raise ApiKeyAuthError("账号已被禁用")

    dept_code = None
    if user.dept_id is not None:
        dept = await get_department(user.dept_id)
        dept_code = dept.code if dept else None

    # 最后使用时间是统计信息，不值得阻塞转发
    asyncio.create_task(_touch_quietly(record.id))

    return Caller(
        uid=user.uid,
        username=user.username,
        role=user.permission,
        dept_id=user.dept_id,
        dept_code=dept_code,
        auth_type="apikey",
        api_key_id=record.id,
        api_key_token_limit=getattr(record, "total_token_limit", None),
        # getattr 兜底：阶段 24 之前建的库没有这两列，读不到时按 private ——
        # 与加列前的行为逐字一致。**这里不能 fail closed 成 group**：那会让
        # 所有存量个人 Key 一夜之间失去 MCP 与 skill，而它们本来就是私聊。
        # fail closed 的落点在 scope_from_credential（认不出来的值→group），
        # 两者不矛盾：这里补的是「列不存在」，那里挡的是「值不认识」。
        scope=getattr(record, "scope", None) or "private",
        scope_project_id=getattr(record, "scope_project_id", None),
    )


async def _revoke_idle_quietly(key_id: int, idle_days: int) -> None:
    from db.updates.apikey import revoke_api_key
    from services.apikey_lifecycle import SYSTEM_ACTOR, idle_reason

    try:
        await revoke_api_key(key_id, reason=idle_reason(idle_days), by=SYSTEM_ACTOR)
    except Exception as exc:
        logger.warning(f"闲置 API Key 吊销失败（巡检会补上）: {exc}")


async def _touch_quietly(key_id: int) -> None:
    try:
        await touch_api_key(key_id)
    except Exception as exc:
        logger.warning(f"更新 API Key 使用时间失败（不影响请求）: {exc}")


def _caller_from_user_info(info: UserInfo) -> Caller:
    return Caller(
        uid=info.uid,
        username=info.username,
        role=info.permission,
        dept_id=info.dept_id,
        dept_code=info.dept_code,
        auth_type="jwt",
    )


async def require_caller(request: Request) -> Caller:
    """转发接口的认证依赖项"""
    token = _bearer_token(request)
    if not token:
        raise ApiKeyAuthError("缺少认证信息：请携带 Authorization: Bearer <API Key 或 access_token>")

    if looks_like_api_key(token):
        return await _caller_from_api_key(token)

    # `/v1` 的 JWT 通道必须与管理端使用同一份有状态撤销真源。只校验签名和
    # 账号状态会让已经登出、被重置密码或因角色/部门调整而撤销的旧 access token
    # 继续访问所有 `/v1` 接口，直到 JWT 自然过期。
    if await token_manager.is_token_revoked(token):
        raise AuthError("身份凭证已失效，请重新登录", code=10002)

    info = await check_token(token)
    if isinstance(info, Response):
        raise AuthError(info.message, code=info.code)

    return _caller_from_user_info(info)


async def optional_caller(request: Request) -> Optional[Caller]:
    """给不强制登录的接口用（目前没有，留作探针类接口的扩展位）"""
    try:
        return await require_caller(request)
    except AuthError:
        return None
