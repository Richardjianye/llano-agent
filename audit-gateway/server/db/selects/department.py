"""部门查询"""

from typing import List, Optional

from db import init_async_session
from model.database import Department, User
from sqlalchemy import func, select


async def get_department(dept_id: int) -> Optional[Department]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(Department).where(Department.id == dept_id))
        ).scalar_one_or_none()


async def get_department_by_code(code: str) -> Optional[Department]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(Department).where(Department.code == code))
        ).scalar_one_or_none()


async def list_departments(include_disabled: bool = False) -> List[Department]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(Department).order_by(Department.id)
        if not include_disabled:
            stmt = stmt.where(Department.status == 1)
        return list((await session.execute(stmt)).scalars().all())


async def count_department_users(dept_id: int) -> int:
    """部门下的用户数 —— 删除部门前的安全检查"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(func.count(User.id)).where(User.dept_id == dept_id)
            )
        ).scalar_one()
