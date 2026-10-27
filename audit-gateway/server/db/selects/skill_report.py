"""技能安全事件查询 —— 阶段 7 · S7-C（管理端只读）

列表刻意**不带样本内容**：样本是员工自己写的源码，一页四十条全带上等于把
几十个人的代码一次性推给任何打得开这个页面的人。要看代码走 /detail，
于是「谁看了谁的代码」至少是一个单独的动作。

（本期没做查阅日志。会话审计那套有「看的人也被看」，这里还没有 —— 见 TODO。）
"""

from typing import Any, Dict, List, Optional, Tuple

from db import init_async_session
from model.database import SkillSecurityEvent, SkillSecuritySample
from sqlalchemy import func, select


def list_conditions(
    *,
    event: Optional[str] = None,
    verdict: Optional[str] = None,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    skill_name: Optional[str] = None,
):
    """筛选条件。抽出来是为了能在没有数据库的情况下直接验它。"""
    conditions = []
    if event:
        conditions.append(SkillSecurityEvent.event == event)
    if verdict:
        conditions.append(SkillSecurityEvent.verdict == verdict)
    if dept_id is not None:
        conditions.append(SkillSecurityEvent.dept_id == dept_id)
    if user_uid:
        conditions.append(SkillSecurityEvent.user_uid == user_uid)
    if skill_name:
        conditions.append(SkillSecurityEvent.skill_name.like(f"%{skill_name}%"))
    return conditions


def list_query(conditions, *, page: int, page_size: int):
    """列表查询语句。

    按 created_at 排，**不按 occurred_at**：后者是客户端上报的字符串，一台机器
    时间被改过（或者时区没设对）就会排到天边去，而这个列表的用途正是「按时间
    看事件」。id 作为第二排序键，避免同一秒内的多条在翻页时来回换位置 ——
    那会让第 2 页漏掉第 1 页尾巴上的行。
    """
    return (
        select(SkillSecurityEvent)
        .where(*conditions)
        .order_by(SkillSecurityEvent.created_at.desc(), SkillSecurityEvent.id.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    )


async def fetch_events(
    *,
    event: Optional[str] = None,
    verdict: Optional[str] = None,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    skill_name: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    conditions = list_conditions(
        event=event, verdict=verdict, dept_id=dept_id,
        user_uid=user_uid, skill_name=skill_name,
    )

    session_maker = await init_async_session()
    async with session_maker() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(SkillSecurityEvent).where(*conditions)
            )
        ).scalar_one()

        rows = (
            await session.execute(list_query(conditions, page=page, page_size=page_size))
        ).scalars().all()

    return [_summary(row) for row in rows], int(total)


def _summary(row: SkillSecurityEvent) -> Dict[str, Any]:
    """列表行。刻意不含 findings_json 与任何样本内容。"""
    return {
        "id": row.id,
        "event": row.event,
        "user_uid": row.user_uid,
        "dept_id": row.dept_id,
        "dept_code": row.dept_code,
        "device": row.device,
        "skill_name": row.skill_name,
        "verdict": row.verdict,
        "source": row.source,
        "scanner_version": row.scanner_version,
        "bundle_hash": row.bundle_hash,
        "finding_count": row.finding_count,
        "has_sample": row.sample_id is not None,
        "occurred_at": row.occurred_at,
        "created_at": row.created_at,
    }


async def fetch_event(event_row_id: int) -> Optional[SkillSecurityEvent]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(SkillSecurityEvent).where(SkillSecurityEvent.id == event_row_id)
            )
        ).scalar_one_or_none()


async def fetch_sample(sample_id: int) -> Optional[SkillSecuritySample]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(SkillSecuritySample).where(SkillSecuritySample.id == sample_id)
            )
        ).scalar_one_or_none()


async def fetch_related_events(bundle_hash: str, *, exclude_id: int) -> List[Dict[str, Any]]:
    """同一个技能包的其它事件。

    详情页要回答的关键问题之一是「这个技能被隔离之后，有多少人把它放回去了」——
    那是一条隔离事件加上 N 条恢复事件，靠 bundle_hash 串起来。
    """
    if not bundle_hash:
        return []

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(SkillSecurityEvent)
                .where(
                    SkillSecurityEvent.bundle_hash == bundle_hash,
                    SkillSecurityEvent.id != exclude_id,
                )
                .order_by(SkillSecurityEvent.created_at.desc())
                .limit(100)
            )
        ).scalars().all()

    return [_summary(row) for row in rows]
