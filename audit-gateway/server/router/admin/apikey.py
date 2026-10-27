"""API Key 管理 —— 供桌面客户端接入网关

每个人管自己的 Key；管理员（级别更高者）可以给下级签发与吊销。
明文只在签发响应里返回一次，库里只有 sha256。
"""

from datetime import datetime, timedelta
from typing import List, Optional

from db.inserts.apikey import insert_api_key
from db.selects.apikey import get_key, get_keys, list_keys, list_keys_overview
from db.selects.user import get_user_info
from db.updates.apikey import revoke_api_key, revoke_api_keys
from fastapi import APIRouter, Depends, Query
from loguru import logger
from model.database import now
from model.response import Response, UserInfo
from pydantic import BaseModel, Field
from services.apikey_lifecycle import (REASON_ADMIN, REASON_ADMIN_BATCH,
                                       STATE_IDLE, STATES, configured_idle_days,
                                       idle_deadline, idle_reason, key_state)
from services.permissions import can_manage
from utils.apikey import generate_api_key
from utils.dependencies import require_login, require_permission
from utils.errors import NotFoundError, PermissionError_, ValidationError_

router = APIRouter(prefix="/apikey", tags=["API Key"])


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="用途备注，如「我的 Mac」")
    # 留空表示不过期；给外包/临时人员发 Key 时建议设个期限
    valid_days: Optional[int] = Field(None, ge=1, le=3650)
    # 精确到分钟的自定义到期时间；与 valid_days 二选一。
    expires_at: Optional[datetime] = None
    # 独立于用户/部门日额度的累计用量上限；留空表示该 Key 不单独限制。
    total_token_limit: Optional[int] = Field(None, ge=1)
    # 管理员给别人发 Key 时填，留空表示给自己发
    target_uid: Optional[str] = None


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _payload(record, idle_days: Optional[int] = None) -> dict:
    idle_days = configured_idle_days() if idle_days is None else idle_days
    return {
        "id": record.id,
        "name": record.name,
        "key_prefix": record.key_prefix,
        "status": record.status,
        # active / revoked / expired / idle —— 与网关认证的拒绝顺序一致
        "state": key_state(record, now(), idle_days),
        "scope": getattr(record, "scope", None) or "private",
        "expires_at": _iso(record.expires_at),
        "idle_expires_at": _iso(idle_deadline(record, idle_days)) if record.status == 1 else None,
        "total_token_limit": record.total_token_limit,
        "used_tokens": int(getattr(record, "used_tokens", 0) or 0),
        "last_used_at": _iso(record.last_used_at),
        "created_at": _iso(record.created_at),
        "revoked_at": _iso(getattr(record, "revoked_at", None)),
        "revoke_reason": getattr(record, "revoke_reason", None),
        "revoked_by": getattr(record, "revoked_by", None),
    }


async def _assert_can_operate(operator: UserInfo, target_uid: str) -> None:
    """操作别人的 Key 需要角色级别更高"""
    if target_uid == operator.uid:
        return

    target = await get_user_info(uid=target_uid)
    if target is None:
        raise NotFoundError("账号不存在")
    if not can_manage(operator.permission, target.permission):
        raise PermissionError_("无权操作该账号的 API Key")


@router.get("/list", response_model=Response)
async def apikey_list(
    uid: Optional[str] = None,
    operator: UserInfo = Depends(require_login),
):
    """列出 Key。默认看自己的；查别人的需要级别更高"""
    target_uid = uid or operator.uid
    await _assert_can_operate(operator, target_uid)

    records = await list_keys(target_uid)
    return Response(data={"items": [_payload(r) for r in records]})


@router.post("/create", response_model=Response)
async def apikey_create(
    body: ApiKeyCreate,
    operator: UserInfo = Depends(require_login),
):
    """签发一把新 Key —— 明文只在这次响应里返回"""
    target_uid = body.target_uid or operator.uid
    await _assert_can_operate(operator, target_uid)

    if body.valid_days is not None and body.expires_at is not None:
        raise ValidationError_("valid_days 与 expires_at 不能同时设置")

    expires_at = body.expires_at
    if expires_at is not None and expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone().replace(tzinfo=None)
    if expires_at is not None and expires_at <= now():
        raise ValidationError_("到期时间必须晚于当前时间")
    if body.valid_days:
        expires_at = now() + timedelta(days=body.valid_days)

    plain, prefix, key_hash = generate_api_key()
    key_id = await insert_api_key(
        user_uid=target_uid,
        name=body.name,
        key_prefix=prefix,
        key_hash=key_hash,
        expires_at=expires_at,
        total_token_limit=body.total_token_limit,
    )

    logger.info(
        f"API Key 已签发: id={key_id} prefix={prefix} owner={target_uid} by={operator.uid}"
    )
    return Response(
        data={"id": key_id, "api_key": plain, "key_prefix": prefix},
        message="请立即保存，此明文只显示一次",
    )


@router.post("/revoke/{key_id}", response_model=Response)
async def apikey_revoke(
    key_id: int,
    operator: UserInfo = Depends(require_login),
):
    """吊销 Key（不物理删，保留审计追溯）"""
    record = await get_key(key_id)
    if record is None:
        raise NotFoundError("API Key 不存在")

    await _assert_can_operate(operator, record.user_uid)

    await revoke_api_key(key_id, reason=REASON_ADMIN, by=operator.uid)
    logger.warning(
        f"API Key 已吊销: id={key_id} prefix={record.key_prefix} by={operator.uid}"
    )
    return Response(message="已吊销")


# ==================== 全公司总览与批量吊销（2026-10-19）====================


class ApiKeyBatchRevoke(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=1000)


class ApiKeyIdleCleanup(BaseModel):
    # 先 dry_run 看会清掉多少，再真清 —— 前端确认框里显示这个数
    dry_run: bool = True


@router.get("/overview", response_model=Response)
async def apikey_overview(
    state: Optional[str] = Query(None, description="逗号分隔：active,idle,expired,revoked"),
    keyword: str = Query("", max_length=64, description="持有人姓名/账号、Key 名称或前缀"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: UserInfo = Depends(require_permission("apikey:admin")),
):
    """全公司 Key 总览，按最后使用时间从久到近排（最该清理的在最上面）"""
    states = tuple(s for s in (state or "").split(",") if s)
    unknown = [s for s in states if s not in STATES]
    if unknown:
        raise ValidationError_(f"state 可选值：{', '.join(STATES)}")

    idle_days = configured_idle_days()
    rows, total = await list_keys_overview(
        states=states, keyword=keyword, idle_days=idle_days, limit=limit, offset=offset
    )
    items = []
    for record, owner in rows:
        item = _payload(record, idle_days)
        item["owner"] = {
            "uid": record.user_uid,
            "name": (owner.realname or owner.username) if owner else record.user_uid,
            "username": owner.username if owner else None,
            "dept_id": owner.dept_id if owner else None,
            "status": owner.status if owner else None,
        }
        items.append(item)
    return Response(data={"items": items, "total": total, "idle_days": idle_days})


async def _manageable_ids(operator: UserInfo, records) -> tuple:
    """筛出操作者有权吊销的 Key；别人的 Key 需要角色级别更高"""
    allowed, denied = [], []
    roles = {}
    for record in records:
        if record.user_uid == operator.uid:
            allowed.append(record.id)
            continue
        if record.user_uid not in roles:
            owner = await get_user_info(uid=record.user_uid)
            roles[record.user_uid] = owner.permission if owner else None
        role = roles[record.user_uid]
        # 账号已删的孤儿 Key 谁都能清
        if role is None or can_manage(operator.permission, role):
            allowed.append(record.id)
        else:
            denied.append(record.id)
    return allowed, denied


@router.post("/batch-revoke", response_model=Response)
async def apikey_batch_revoke(
    body: ApiKeyBatchRevoke,
    operator: UserInfo = Depends(require_permission("apikey:admin")),
):
    """勾选批量吊销。无权操作的（如总裁的 Key）跳过并回报，不整批失败"""
    records = await get_keys(body.ids)
    allowed, denied = await _manageable_ids(operator, records)
    revoked = await revoke_api_keys(allowed, reason=REASON_ADMIN_BATCH, by=operator.uid)
    logger.warning(f"API Key 批量吊销: revoked={revoked} denied={denied} by={operator.uid}")
    return Response(data={"revoked": revoked, "denied_ids": denied})


@router.post("/revoke-idle", response_model=Response)
async def apikey_revoke_idle(
    body: ApiKeyIdleCleanup,
    operator: UserInfo = Depends(require_permission("apikey:admin")),
):
    """立刻吊销全部闲置 Key（reason=idle_30d，revoked_by=操作者）

    平时由后台巡检每小时自动做（revoked_by=system），这个按钮是「现在就清」。
    闲置的 Key 在认证时本来就会被拒绝，这一步不会让任何还在续期的客户端掉线。
    """
    idle_days = configured_idle_days()
    if idle_days <= 0:
        raise ValidationError_("闲置失效已关闭（API_KEY_IDLE_DAYS=0）")
    rows, _total = await list_keys_overview(states=(STATE_IDLE,), idle_days=idle_days, limit=100000)
    allowed, denied = await _manageable_ids(operator, [r for r, _ in rows])
    if body.dry_run:
        return Response(data={"would_revoke": len(allowed), "denied_ids": denied, "idle_days": idle_days})
    revoked = await revoke_api_keys(allowed, reason=idle_reason(idle_days), by=operator.uid)
    logger.warning(f"API Key 闲置清理: revoked={revoked} idle_days={idle_days} by={operator.uid}")
    return Response(data={"revoked": revoked, "denied_ids": denied, "idle_days": idle_days})
