"""部门更新"""

from typing import Optional

from db import init_async_session
from model.database import Department, User
from sqlalchemy import update


async def update_department(
    dept_id: int,
    name: Optional[str] = None,
    parent_id: Optional[int] = None,
    status: Optional[int] = None,
    remark: Optional[str] = None,
    dingtalk_dept_id: Optional[str] = None,
) -> bool:
    """按需更新部门字段，返回是否有实际写入"""
    data = {}
    if name is not None:
        data["name"] = name
    if parent_id is not None:
        data["parent_id"] = parent_id
    if status is not None:
        data["status"] = status
    if remark is not None:
        data["remark"] = remark
    if dingtalk_dept_id is not None:
        data["dingtalk_dept_id"] = dingtalk_dept_id

    if not data:
        return False

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(Department).where(Department.id == dept_id).values(data)
        )
        await session.commit()
    return True


async def move_users_to_department(from_dept_id: int, to_dept_id: Optional[int]) -> None:
    """批量转移部门下的用户（停用部门前使用）"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(User).where(User.dept_id == from_dept_id).values(dept_id=to_dept_id)
        )
        await session.commit()
