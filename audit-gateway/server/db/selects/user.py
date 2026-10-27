"""封装常用用户查询方法"""

from db import init_async_session
from model.database import Department, UsageRecord, User
from sqlalchemy import case, exists, func, or_, select


USER_SORT_FIELDS = frozenset({
    "realname",
    "permission",
    "dept_id",
    "status",
    "last_login_at",
    "last_request_at",
})


def _apply_user_filters(stmt, *, dept_id=None, role=None, keyword=None):
    if dept_id is not None:
        stmt = stmt.where(User.dept_id == dept_id)
    if role is not None:
        stmt = stmt.where(User.permission == role)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(User.username.like(like), User.realname.like(like))
        )
    return stmt


def build_user_count_query(*, dept_id=None, role=None, keyword=None):
    """列表与总数必须共享同一组过滤条件。"""
    return _apply_user_filters(
        select(func.count(User.id)),
        dept_id=dept_id,
        role=role,
        keyword=keyword,
    )


def build_latest_chat_request_query(user_uids: list[str]):
    """构造本页用户最近一次真实对话请求查询。"""
    return (
        select(
            UsageRecord.user_uid,
            func.max(UsageRecord.created_at),
        )
        .where(
            UsageRecord.user_uid.in_(user_uids),
            UsageRecord.call_kind == "chat",
        )
        .group_by(UsageRecord.user_uid)
    )


def _latest_chat_request_column():
    """每个账号最近一次真实对话请求，供全局排序使用。"""
    return (
        select(func.max(UsageRecord.created_at))
        .where(
            UsageRecord.user_uid == User.uid,
            UsageRecord.call_kind == "chat",
        )
        .correlate(User)
        .scalar_subquery()
    )


def build_user_list_query(
    *,
    dept_id=None,
    role=None,
    keyword=None,
    sort_by: str = "id",
    sort_order: str = "asc",
):
    """构造账号列表查询；排序必须在分页前完成。

    Vuetify 的服务端表格只会把排序意图交给接口。这里使用固定映射而不是
    ``getattr(User, sort_by)``，避免把任意列名甚至 SQL 片段带进查询。
    """
    normalized_sort = sort_by if sort_by in USER_SORT_FIELDS else "id"
    normalized_order = "desc" if sort_order == "desc" else "asc"
    latest_request = _latest_chat_request_column()

    columns = {
        "id": User.id,
        "realname": func.coalesce(func.nullif(User.realname, ""), User.username),
        "permission": User.permission,
        "dept_id": Department.name,
        "status": User.status,
        "last_login_at": User.last_login_at,
        "last_request_at": latest_request,
    }
    column = columns[normalized_sort]

    stmt = select(User).outerjoin(Department, Department.id == User.dept_id)
    stmt = _apply_user_filters(
        stmt,
        dept_id=dept_id,
        role=role,
        keyword=keyword,
    )

    # MySQL 不支持 ``NULLS LAST`` 语法，用 CASE 明确表达，升降序都把空值放末尾。
    direction = column.desc() if normalized_order == "desc" else column.asc()
    return stmt.order_by(case((column.is_(None), 1), else_=0), direction, User.id.asc())


def build_user_lookup_conditions(uid: str = None, id: int = None):
    """按传入值动态拼查询条件

    早期版本写死 `or_(User.uid == uid, User.id == id)`，只传一个参数时另一个会
    退化成 `IS NULL`，可能命中脏数据行。抽出来是为了能单测（见 tests/test_user_lookup.py）。
    """
    conditions = []
    if uid is not None:
        conditions.append(User.uid == uid)
    if id is not None:
        conditions.append(User.id == id)
    return conditions


async def get_user_info(uid: str = None, id: int = None):
    """根据 uid 或自增 id 获取用户

    **按用户名登录请用 get_user_by_username**，别把用户名塞进 id ——
    那会变成拿字符串比对整型主键，永远查不到人。
    """
    conditions = build_user_lookup_conditions(uid, id)
    if not conditions:
        return None

    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(User).where(or_(*conditions)))
        ).scalar_one_or_none()


async def get_user_by_username(username: str):
    """按登录用户名取用户（用户名可能是中文花名）"""
    if not username:
        return None
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()


async def check_username(username: str):
    """检查用户名是否已存在"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(exists().where(User.username == username)))
        ).scalar()


async def list_users(
    dept_id: int = None,
    role: str = None,
    keyword: str = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "id",
    sort_order: str = "asc",
):
    """分页查询用户，支持过滤与服务端全局排序。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = build_user_list_query(
            dept_id=dept_id,
            role=role,
            keyword=keyword,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        stmt = stmt.limit(limit).offset(offset)
        return list((await session.execute(stmt)).scalars().all())


async def count_users(
    dept_id: int = None,
    role: str = None,
    keyword: str = None,
) -> int:
    """用户总数（配合 list_users 分页）"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = build_user_count_query(
            dept_id=dept_id,
            role=role,
            keyword=keyword,
        )
        return (await session.execute(stmt)).scalar_one()


async def latest_chat_request_at(user_uids: list[str]) -> dict[str, object]:
    """返回本页用户最近一次真实对话请求时间。

    `usage_records` 是请求事实来源。后台 gate 打分和 Agent 自主调用不代表用户
    当时活跃，因此只统计 ``call_kind=chat``；成功、失败和中断都仍是实际发生过
    的请求，不按 status 过滤。
    """
    normalized = list(dict.fromkeys(uid for uid in user_uids if uid))
    if not normalized:
        return {}

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                build_latest_chat_request_query(normalized)
            )
        ).all()
    return {uid: requested_at for uid, requested_at in rows}


async def search_directory(
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """全员通讯录用的查询 —— **只回在职账号**，且不带任何权限字段。

    与 `list_users` 分开写，不是重复：`list_users` 是给管理端用的，它必须能看到
    停用账号、能按角色筛。通讯录不该有这两样 —— 停用的人不该还能被拉进项目，
    而「谁是审计员」不是全员该知道的事。合成一个函数再加参数的话，两边的默认值
    迟早有一次会填反，而填反的方向是**多给**。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(User).where(User.status == 1).order_by(User.id)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                or_(User.username.like(like), User.realname.like(like))
            )
        stmt = stmt.limit(max(1, min(int(limit), 200))).offset(max(0, int(offset)))
        return list((await session.execute(stmt)).scalars().all())


async def count_directory(keyword: str = "") -> int:
    from sqlalchemy import func

    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(func.count(User.id)).where(User.status == 1)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                or_(User.username.like(like), User.realname.like(like))
            )
        return (await session.execute(stmt)).scalar_one()
