"""封装常用用户更新方法"""

from db import init_async_session
from model.database import User
from sqlalchemy import update


async def update_user_info(item: User, *, fields: set[str] | None = None):
    """更新用户基本信息。

    ``fields`` 为空时保留旧的「非 None 才更新」行为；传入字段集合时按 PATCH
    语义更新，允许把 ``dept_id``、姓名或昵称显式写成 SQL NULL。调用方必须用请求
    模型的 ``model_fields_set`` 区分「省略」和「明确传 null」。
    """
    data = {}
    explicit_fields = fields or set()
    if item.nickname is not None or "nickname" in explicit_fields:
        data["nickname"] = item.nickname
    if item.realname is not None or "realname" in explicit_fields:
        data["realname"] = item.realname
    if item.username is not None or "username" in explicit_fields:
        data["username"] = item.username
    if item.status is not None or "status" in explicit_fields:
        data["status"] = item.status
    if item.dept_id is not None or "dept_id" in explicit_fields:
        data["dept_id"] = item.dept_id
    if item.permission is not None or "permission" in explicit_fields:
        data["permission"] = item.permission

    if data:
        session_maker = await init_async_session()
        async with session_maker() as session:
            await session.execute(
                update(User).where(User.uid == item.uid).values(data)
            )
            await session.commit()


async def update_user_status(uid: str, status: int):
    """启用/禁用账号"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(User).where(User.uid == uid).values(status=status)
        )
        await session.commit()


async def update_user_last_login(uid: str):
    """记录最后登录时间"""
    from model.database import now

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(User).where(User.uid == uid).values(last_login_at=now())
        )
        await session.commit()


async def update_user_password(item: User):
    """更新用户密码"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(User).where(User.uid == item.uid).values(
                password=item.password)
        )
        await session.commit()
