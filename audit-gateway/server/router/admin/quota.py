"""每日 token 配额管理（总裁/开发者）。"""

from typing import Literal, Optional

from db.selects.catalog import get_model
from db.selects.department import get_department
from db.selects.user import get_user_info
from fastapi import APIRouter, Depends
from loguru import logger
from model.response import Response, UserInfo
from pydantic import BaseModel, Field
from services.quota import (QUOTA_SCOPE_DEPT, QUOTA_SCOPE_USER,
                            delete_quota_rule, find_quota_rule, get_quota_rule,
                            list_quota_rules, quota_status_for_rule,
                            save_quota_rule)
from utils.dependencies import require_permission
from utils.errors import ConflictError, NotFoundError, ValidationError_

router = APIRouter(prefix="/quota", tags=["配额管理"])


class QuotaSave(BaseModel):
    id: Optional[int] = None
    scope: Literal["user", "dept"]
    scope_value: str = Field(..., min_length=1, max_length=64)
    model_id: int = Field(..., gt=0)
    daily_token_limit: int = Field(..., gt=0, le=1_000_000_000_000)
    status: int = Field(1, ge=0, le=1)
    remark: Optional[str] = Field(None, max_length=255)


async def _validate_target(body: QuotaSave) -> str:
    if not await get_model(body.model_id):
        raise NotFoundError("模型不存在")
    if body.scope == QUOTA_SCOPE_USER:
        user = await get_user_info(uid=body.scope_value)
        if not user:
            raise NotFoundError("账号不存在")
        return user.realname or user.username or user.uid
    try:
        dept_id = int(body.scope_value)
    except ValueError:
        raise ValidationError_("部门作用域必须使用部门 ID")
    dept = await get_department(dept_id)
    if not dept:
        raise NotFoundError("部门不存在")
    return dept.name


@router.get("/list", response_model=Response)
async def quota_list(_: UserInfo = Depends(require_permission("model:view"))):
    items = []
    for rule in await list_quota_rules():
        status = await quota_status_for_rule(rule)
        model = await get_model(rule.model_id)
        if rule.scope == QUOTA_SCOPE_USER:
            target = await get_user_info(uid=rule.scope_value)
            scope_name = (target.realname or target.username) if target else rule.scope_value
        else:
            target = await get_department(int(rule.scope_value))
            scope_name = target.name if target else rule.scope_value
        items.append({
            "id": rule.id, "scope": rule.scope, "scope_value": rule.scope_value,
            "scope_name": scope_name, "model_id": rule.model_id,
            "model_name": (model.display_name if model else f"#{rule.model_id}"),
            "daily_token_limit": status.limit, "used_tokens": status.used,
            "remaining_tokens": status.remaining, "exceeded": status.exceeded,
            "status": rule.status, "remark": rule.remark,
        })
    return Response(data={"items": items})


@router.post("/save", response_model=Response)
async def quota_save(
    body: QuotaSave,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    if body.id is not None and not await get_quota_rule(body.id):
        raise NotFoundError("配额规则不存在")
    scope_name = await _validate_target(body)
    duplicate = await find_quota_rule(body.scope, body.scope_value, body.model_id)
    if duplicate is not None and duplicate.id != body.id:
        raise ConflictError("该对象与模型已经配置过配额")
    rule_id = await save_quota_rule(
        rule_id=body.id,
        scope=body.scope,
        scope_value=body.scope_value,
        model_id=body.model_id,
        daily_token_limit=body.daily_token_limit,
        status=body.status,
        remark=body.remark,
    )
    logger.info(
        f"配额已保存 id={rule_id} target={scope_name} "
        f"model={body.model_id} by={operator.uid}"
    )
    return Response(data={"id": rule_id})


@router.post("/delete/{rule_id}", response_model=Response)
async def quota_delete(
    rule_id: int,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    if not await get_quota_rule(rule_id):
        raise NotFoundError("配额规则不存在")
    await delete_quota_rule(rule_id)
    logger.warning(f"配额已删除 id={rule_id} by={operator.uid}")
    return Response(message="已删除")
