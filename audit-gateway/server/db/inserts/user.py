"""封装常用用户插入方法"""

from db import init_async_session
from model.database import ROLE_EMPLOYEE, User
from sqlalchemy import insert


async def insert_user(user: User):
    """插入新用户。password 字段必须是已 bcrypt 哈希后的值"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(User).values(
                uid=user.uid,
                username=user.username,
                password=user.password,
                nickname=user.nickname,
                realname=user.realname,
                permission=user.permission or ROLE_EMPLOYEE,
                dept_id=user.dept_id,
                status=user.status,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]
