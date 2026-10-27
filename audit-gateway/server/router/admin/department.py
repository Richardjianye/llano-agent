"""部门管理路由 —— 权限点 dept:view / dept:edit

调整用户归属的接口在 router/admin/account.py（需要按角色级别校验）。
"""

from db.inserts.department import insert_department
from db.selects.department import (count_department_users, get_department,
                                   get_department_by_code, list_departments)
from db.selects.user import list_users
from db.updates.department import move_users_to_department, update_department
from fastapi import APIRouter, Depends
from loguru import logger
from model.request.org import DepartmentCreate, DepartmentUpdate
from model.response import DepartmentInfo, Response, UserInfo
from utils.dependencies import require_permission
from utils.errors import ConflictError, NotFoundError, ValidationError_

router = APIRouter(prefix="/department", tags=["部门管理"])


def _to_info(dept) -> DepartmentInfo:
    return DepartmentInfo(
        id=dept.id,
        code=dept.code,
        dingtalk_dept_id=dept.dingtalk_dept_id,
        name=dept.name,
        parent_id=dept.parent_id,
        status=dept.status,
        remark=dept.remark,
    )


@router.get("/list", response_model=Response)
async def department_list(
    include_disabled: bool = False,
    _: UserInfo = Depends(require_permission("dept:view")),
):
    """部门列表"""
    depts = await list_departments(include_disabled=include_disabled)
    return Response(data={"items": [_to_info(d).model_dump() for d in depts]})


@router.post("/create", response_model=Response)
async def department_create(
    body: DepartmentCreate,
    operator: UserInfo = Depends(require_permission("dept:edit")),
):
    """新建部门"""
    if await get_department_by_code(body.code):
        raise ConflictError(f"部门编码 {body.code} 已存在")

    if body.parent_id is not None and not await get_department(body.parent_id):
        raise NotFoundError("上级部门不存在")

    dept_id = await insert_department(
        code=body.code,
        name=body.name,
        parent_id=body.parent_id,
        remark=body.remark,
    )
    logger.info(f"部门已创建: id={dept_id} code={body.code} by={operator.uid}")
    return Response(data={"id": dept_id})


@router.post("/update/{dept_id}", response_model=Response)
async def department_update(
    dept_id: int,
    body: DepartmentUpdate,
    operator: UserInfo = Depends(require_permission("dept:edit")),
):
    """修改部门。停用部门前要求该部门下无用户，避免用户失去模型授权后无人察觉"""
    dept = await get_department(dept_id)
    if not dept:
        raise NotFoundError("部门不存在")

    if body.parent_id is not None:
        if body.parent_id == dept_id:
            raise ValidationError_("上级部门不能是自己")
        if not await get_department(body.parent_id):
            raise NotFoundError("上级部门不存在")

    if body.status == 0:
        user_count = await count_department_users(dept_id)
        if user_count > 0:
            raise ValidationError_(
                f"该部门下仍有 {user_count} 个用户，请先转移后再停用"
            )

    await update_department(
        dept_id,
        name=body.name,
        parent_id=body.parent_id,
        status=body.status,
        remark=body.remark,
        dingtalk_dept_id=body.dingtalk_dept_id,
    )
    logger.info(f"部门已更新: id={dept_id} by={operator.uid}")
    return Response()


@router.get("/users/{dept_id}", response_model=Response)
async def department_users(
    dept_id: int,
    limit: int = 50,
    offset: int = 0,
    _: UserInfo = Depends(require_permission("dept:view")),
):
    """查看部门成员"""
    if not await get_department(dept_id):
        raise NotFoundError("部门不存在")

    users = await list_users(dept_id=dept_id, limit=limit, offset=offset)
    return Response(
        data={
            "items": [
                {
                    "uid": u.uid,
                    "username": u.username,
                    "realname": u.realname,
                    "permission": u.permission,
                    "status": u.status,
                }
                for u in users
            ]
        }
    )


@router.post("/transfer", response_model=Response)
async def department_transfer(
    from_dept_id: int,
    to_dept_id: int = None,
    operator: UserInfo = Depends(require_permission("dept:edit")),
):
    """批量转移部门成员（to_dept_id 留空表示移出部门）"""
    if not await get_department(from_dept_id):
        raise NotFoundError("源部门不存在")
    if to_dept_id is not None and not await get_department(to_dept_id):
        raise NotFoundError("目标部门不存在")

    await move_users_to_department(from_dept_id, to_dept_id)
    logger.info(
        f"部门成员批量转移: {from_dept_id} -> {to_dept_id} by={operator.uid}"
    )
    return Response()
