"""API Key 查询"""

from typing import List, Optional

from db import init_async_session
from model.database import ApiKey, UsageRecord, now
from sqlalchemy import func, select


async def get_key_by_prefix(prefix: str) -> Optional[ApiKey]:
    """按前缀定位（前缀列有唯一索引，命中后再比对哈希）"""
    if not prefix:
        return None
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
        ).scalar_one_or_none()


async def list_keys(user_uid: str) -> List[ApiKey]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        usage_totals = (
            select(
                UsageRecord.api_key_id,
                func.sum(UsageRecord.total_tokens).label("used_tokens"),
            )
            .where(UsageRecord.api_key_id.is_not(None))
            .group_by(UsageRecord.api_key_id)
            .subquery()
        )
        rows = (await session.execute(
            select(
                ApiKey,
                func.coalesce(usage_totals.c.used_tokens, 0).label("used_tokens"),
            )
            .outerjoin(usage_totals, usage_totals.c.api_key_id == ApiKey.id)
            .where(ApiKey.user_uid == user_uid)
            .order_by(ApiKey.id.desc())
        )).all()
        records = []
        for record, used_tokens in rows:
            record.used_tokens = int(used_tokens or 0)
            records.append(record)
        return records


async def api_key_used_tokens(key_id: int) -> int:
    """返回一把 Key 在所有模型上的累计 token 用量。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        return int((await session.execute(
            select(func.coalesce(func.sum(UsageRecord.total_tokens), 0))
            .where(UsageRecord.api_key_id == key_id)
        )).scalar_one())


async def get_key(key_id: int) -> Optional[ApiKey]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(ApiKey).where(ApiKey.id == key_id))
        ).scalar_one_or_none()


async def list_keys_overview(
    *,
    states=(),
    keyword: str = "",
    current=None,
    idle_days: int = 0,
    limit: int = 50,
    offset: int = 0,
):
    """全公司 Key 总览：(Key, 所属账号) 列表与总数

    状态是算出来的（吊销/过期/闲置/有效），没有单独的列；几百行的表，按状态过滤
    在 SQL 里写成与 apikey_lifecycle.key_state 同口径的条件。
    """
    from datetime import timedelta

    from model.database import User
    from services.apikey_lifecycle import (STATE_ACTIVE, STATE_EXPIRED,
                                           STATE_IDLE, STATE_REVOKED)
    from services.member_search import escape_like
    from sqlalchemy import and_, or_

    current = current or now()
    last_seen = func.coalesce(ApiKey.last_used_at, ApiKey.created_at)
    live = ApiKey.status == 1
    not_expired = or_(ApiKey.expires_at.is_(None), ApiKey.expires_at >= current)
    if idle_days > 0:
        idle = and_(ApiKey.scope == "private", last_seen < current - timedelta(days=idle_days))
    else:
        idle = ApiKey.id.is_(None)  # 恒假
    conditions = {
        STATE_REVOKED: ApiKey.status != 1,
        STATE_EXPIRED: and_(live, ApiKey.expires_at.is_not(None), ApiKey.expires_at < current),
        STATE_IDLE: and_(live, not_expired, idle),
        STATE_ACTIVE: and_(live, not_expired, ~idle),
    }

    stmt = select(ApiKey, User).outerjoin(User, User.uid == ApiKey.user_uid)
    count = select(func.count(ApiKey.id)).select_from(ApiKey).outerjoin(User, User.uid == ApiKey.user_uid)
    filters = []
    if states:
        filters.append(or_(*(conditions[s] for s in states)))
    if keyword:
        like = f"%{escape_like(keyword.strip())}%"
        filters.append(or_(
            User.realname.like(like, escape="\\"),
            User.username.like(like, escape="\\"),
            ApiKey.name.like(like, escape="\\"),
            ApiKey.key_prefix.like(like, escape="\\"),
        ))
    for f in filters:
        stmt = stmt.where(f)
        count = count.where(f)

    stmt = stmt.order_by(last_seen.asc(), ApiKey.id).limit(limit).offset(offset)
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(stmt)).all()
        total = int((await session.execute(count)).scalar_one())
    return rows, total


async def get_keys(key_ids) -> List[ApiKey]:
    ids = sorted({int(i) for i in key_ids})
    if not ids:
        return []
    session_maker = await init_async_session()
    async with session_maker() as session:
        return list((await session.execute(select(ApiKey).where(ApiKey.id.in_(ids)))).scalars().all())


async def idle_key_ids(cutoff) -> List[int]:
    """仍有效、且最后一次使用（没用过按签发时间）早于 cutoff 的个人 Key"""
    last_seen = func.coalesce(ApiKey.last_used_at, ApiKey.created_at)
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = await session.execute(
            select(ApiKey.id).where(
                ApiKey.status == 1,
                ApiKey.scope == "private",
                last_seen < cutoff,
            )
        )
        return [int(r[0]) for r in rows]
