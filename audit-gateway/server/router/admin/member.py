"""成员搜索 —— 管理端各「选人」控件的唯一数据源

契约见 server/MEMBER_SEARCH_API.md。对外（OSS / 第三方平台）**不走这个路由**：
这里认的是管理员登录态；对外是 router/open_api.py，用应用凭据、只回 uid / 昵称 / 状态。
"""

from fastapi import APIRouter, Depends, Query
from model.database import ALL_ROLES
from model.response import Response, UserInfo
from services.member_search import (STATUSES, MemberQuery, expand_dept_filter,
                                    parse_csv, parse_int_csv, roles_valid,
                                    search_members)
from utils.dependencies import require_permission
from utils.errors import ValidationError_

router = APIRouter(prefix="/member", tags=["成员搜索"])


@router.get("/search", response_model=Response)
async def member_search(
    keyword: str = Query("", max_length=64, description="姓名/账号/昵称包含匹配；uid、钉钉 userId 精确匹配"),
    dept_ids: str = Query(None, description="部门 ID，逗号分隔"),
    include_descendants: bool = Query(True, description="部门过滤是否包含全部下级部门"),
    roles: str = Query(None, description="角色，逗号分隔"),
    status: str = Query("active", description="active / disabled / all"),
    uids: str = Query(None, description="按 uid 精确取回，逗号分隔，最多 200 个（回显已选值）"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: UserInfo = Depends(require_permission("member:search")),
):
    """分页搜索全量成员"""
    if status not in STATUSES:
        raise ValidationError_(f"status 可选值：{', '.join(STATUSES)}")
    role_list = parse_csv(roles)
    if not roles_valid(role_list, ALL_ROLES):
        raise ValidationError_(f"角色非法，可选值：{', '.join(ALL_ROLES)}")
    try:
        dept_list = parse_int_csv(dept_ids)
    except ValueError as exc:
        raise ValidationError_(str(exc))

    query = MemberQuery(
        keyword=keyword,
        dept_ids=await expand_dept_filter(dept_list, include_descendants) if dept_list else (),
        roles=role_list,
        status=status,
        uids=parse_csv(uids),
        limit=limit,
        offset=offset,
    )
    page = await search_members(query)
    return Response(data=page.as_dict())
