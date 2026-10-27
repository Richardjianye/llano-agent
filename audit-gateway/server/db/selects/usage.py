"""用量查询 —— 报表的读取侧

分工很明确，写代码时别越界：

- **报表、趋势、下钻**一律查 `usage_daily`（日期 × 用户 × 模型 已预聚合）。
- **只有明细页**才查 `usage_records`，而且强制分页 + 强制带时间范围。

明细表随调用量线性增长，半年后随手一个不带条件的查询就能把库拖垮
（这是 TODO 风险表里已经写明的中级风险），所以这里的每个函数都强制带时间范围。

数据范围（谁能看到谁的数据）由 services/permissions.py 的 resolve_usage_scope 算出，
在这里翻译成 WHERE 条件。**过滤条件绝不能信前端传的值** ——
前端传的 dept_id 只是「想看哪个部门」，能不能看由 scope 说了算。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from db import init_async_session
from model.database import (Department, ModelInfo, UsageDaily, UsageRecord,
                            User)
from services.permissions import (USAGE_SCOPE_ALL, USAGE_SCOPE_DEPT,
                                  UsageScope)
from services.usage_query import DailyRow, ModelPrice
from sqlalchemy import func, select


def _apply_scope(stmt, table, scope: UsageScope):
    """把数据范围翻译成 WHERE —— 所有用量查询的必经之路

    部门范围是**本部门 + 所有下级**（2026-09-02）。在这之前是一个等号，
    于是「客户服务中心I」的负责人打开报表只看到 1 个人，而他手下有 48 个。
    """
    if scope.scope == USAGE_SCOPE_ALL:
        return stmt
    if scope.scope == USAGE_SCOPE_DEPT:
        if not scope.dept_ids:
            # 空集合是**错误状态**，不是「不限制」。走到这里说明路由层忘了调
            # org_tree 展开。当场报错，而不是二选一地默默犯错：
            # 放行 = 部门负责人看到全公司；空过滤 = 他以为部门没产生任何用量。
            raise ValueError(
                "部门范围没有展开：UsageScope.dept_ids 为空。"
                "路由层必须先用 services.org_tree.dept_scope_ids 展开再查询。"
            )
        return stmt.where(table.dept_id.in_(scope.dept_ids))
    # 仅自己
    return stmt.where(table.user_uid == scope.user_uid)


async def fetch_daily_rows(
    start: date,
    end: date,
    scope: UsageScope,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
) -> List[DailyRow]:
    """查日聚合表，返回纯数据结构交给 services/usage_query.py 去聚合

    在 Python 里做二次聚合而不是写复杂 SQL：维度组合有 5 种、粒度有 3 种，
    拼 SQL 会变成一坨 if；日聚合表本身量级已经很小（人数 × 模型数 × 天数），
    拉回来算完全扛得住，且这段逻辑能脱离数据库单测。
    真到了扛不住的那天，再针对最热的两三种组合下推 SQL 也不迟。
    """
    stmt = select(UsageDaily).where(
        UsageDaily.stat_date >= start, UsageDaily.stat_date <= end
    )
    stmt = _apply_scope(stmt, UsageDaily, scope)

    # 下面这些是用户主动选的筛选条件，只能在 scope 的范围内进一步收窄
    if dept_id is not None:
        stmt = stmt.where(UsageDaily.dept_id == dept_id)
    if user_uid:
        stmt = stmt.where(UsageDaily.user_uid == user_uid)
    if role:
        stmt = stmt.where(UsageDaily.role == role)
    if model_id is not None:
        stmt = stmt.where(UsageDaily.model_id == model_id)

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(stmt)).scalars().all()

    return [
        DailyRow(
            stat_date=r.stat_date,
            user_uid=r.user_uid,
            dept_id=r.dept_id,
            role=r.role,
            model_id=r.model_id,
            model_name=r.model_name,
            requests=r.requests or 0,
            success_count=r.success_count or 0,
            failed_count=r.failed_count or 0,
            prompt_tokens=r.prompt_tokens or 0,
            completion_tokens=r.completion_tokens or 0,
            total_tokens=r.total_tokens or 0,
            latency_ms_sum=r.latency_ms_sum or 0,
            estimated_requests=r.estimated_requests or 0,
            estimated_tokens=r.estimated_tokens or 0,
            cached_prompt_tokens=r.cached_prompt_tokens or 0,
            # 单价快照三件套要一起带上，少一个 row_cost() 就判断不出
            # 这一行的费用能不能用（详见那个函数的注释）
            cost_sum=Decimal(r.cost_sum or 0),
            snapshot_requests=r.snapshot_requests or 0,
            priced_requests=r.priced_requests or 0,
        )
        for r in rows
    ]


async def fetch_model_prices() -> Dict[int, ModelPrice]:
    """模型的**当前**单价 —— 只用于给没有单价快照的历史数据回算费用

    ⚠️ 拿今天的价乘历史的 token，厂商一调价，上个月那份报表再打开金额就变了。
    所以从「单价快照」上线之后，新数据一律用记账时存下的价（见
    services/usage.build_price_snapshot 与 usage_query.row_cost），
    这个函数只兜底那之前的历史行，且响应里会用 cost_basis 标出来。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(
                    ModelInfo.id,
                    ModelInfo.input_price_per_1m,
                    ModelInfo.output_price_per_1m,
                    ModelInfo.cached_input_price_per_1m,
                    ModelInfo.price_per_call,
                    ModelInfo.billing_mode,
                    ModelInfo.currency,
                )
            )
        ).all()

    # 字段映射统一走 ModelPrice.from_model：写入侧快照也用它。
    # 两边各抄一份的话，日后加一档单价必然漏掉一边，而漏的那边不会报错
    return {row.id: ModelPrice.from_model(row) for row in rows}


async def fetch_user_labels(uids: List[str]) -> Dict[str, str]:
    """uid → 展示名。报表里给人看的必须是花名，不是一串 uuid"""
    if not uids:
        return {}

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(User.uid, User.username, User.realname).where(
                    User.uid.in_(uids)
                )
            )
        ).all()

    # 账号删了但历史用量还在 —— 查不到的 uid 由调用方回退显示原值
    return {row[0]: (row[2] or row[1] or row[0]) for row in rows}


async def fetch_dept_labels(dept_ids: List[int]) -> Dict[int, str]:
    """部门ID → 部门名"""
    if not dept_ids:
        return {}

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(Department.id, Department.name).where(
                    Department.id.in_(dept_ids)
                )
            )
        ).all()

    return {row[0]: row[1] for row in rows}


async def fetch_records(
    start: date,
    end: date,
    scope: UsageScope,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[UsageRecord], int]:
    """明细分页 —— 唯一会碰 usage_records 的查询

    返回 (当前页, 总数)。时间范围是必填参数而不是可选：
    这张表会长到千万级，允许「不带时间查一下」就等于允许把库查挂。
    """
    # stat_date 是 Date，用它过滤能吃到索引；再按 created_at 排序保证同一天内有序
    conditions = [UsageRecord.stat_date >= start, UsageRecord.stat_date <= end]

    stmt = select(UsageRecord).where(*conditions)
    count_stmt = select(func.count(UsageRecord.id)).where(*conditions)

    stmt = _apply_scope(stmt, UsageRecord, scope)
    count_stmt = _apply_scope(count_stmt, UsageRecord, scope)

    for column, value in (
        (UsageRecord.dept_id, dept_id),
        (UsageRecord.user_uid, user_uid or None),
        (UsageRecord.role, role or None),
        (UsageRecord.model_id, model_id),
        (UsageRecord.status, status or None),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
            count_stmt = count_stmt.where(column == value)

    stmt = stmt.order_by(UsageRecord.id.desc()).limit(limit).offset(offset)

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = list((await session.execute(stmt)).scalars().all())
        total = (await session.execute(count_stmt)).scalar_one()

    return rows, total


async def fetch_intraday_records(
    day: date,
    scope: UsageScope,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
) -> List[UsageRecord]:
    """读取单个服务器本地日的明细，供 5 分钟趋势聚合。

    这是报表读取明细表的唯一非分页例外，但被 ``stat_date = day`` 硬限制，
    能命中日期索引，绝不接受任意历史区间。
    """
    stmt = select(UsageRecord).where(UsageRecord.stat_date == day)
    stmt = _apply_scope(stmt, UsageRecord, scope)

    for column, value in (
        (UsageRecord.dept_id, dept_id),
        (UsageRecord.user_uid, user_uid or None),
        (UsageRecord.role, role or None),
        (UsageRecord.model_id, model_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)

    session_maker = await init_async_session()
    async with session_maker() as session:
        return list((await session.execute(stmt)).scalars().all())


async def count_active_chat_users(
    start: date,
    end: date,
    scope: UsageScope,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
) -> int:
    """按真实 chat 请求统计活跃人数；心跳、gate 与 agent 自主调用都不算。"""
    stmt = select(func.count(func.distinct(UsageRecord.user_uid))).where(
        UsageRecord.stat_date >= start,
        UsageRecord.stat_date <= end,
        UsageRecord.call_kind == "chat",
    )
    stmt = _apply_scope(stmt, UsageRecord, scope)
    for column, value in (
        (UsageRecord.dept_id, dept_id),
        (UsageRecord.user_uid, user_uid or None),
        (UsageRecord.role, role or None),
        (UsageRecord.model_id, model_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)

    session_maker = await init_async_session()
    async with session_maker() as session:
        return int((await session.execute(stmt)).scalar_one())


async def fetch_filter_options(scope: UsageScope) -> Dict[str, list]:
    """报表筛选下拉框的候选项

    只列出「范围内确实产生过用量」的维度值，避免下拉框里挂着一堆
    选了也查不出东西的部门和模型。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        dept_stmt = _apply_scope(
            select(UsageDaily.dept_id).distinct(), UsageDaily, scope
        )
        model_stmt = _apply_scope(
            select(UsageDaily.model_id, UsageDaily.model_name).distinct(),
            UsageDaily,
            scope,
        )

        dept_ids = [
            row[0] for row in (await session.execute(dept_stmt)).all()
            if row[0] is not None
        ]
        models = [
            {"id": row[0], "name": row[1]}
            for row in (await session.execute(model_stmt)).all()
            if row[0] is not None
        ]

    dept_labels = await fetch_dept_labels(dept_ids)

    return {
        "departments": [
            {"id": did, "name": dept_labels.get(did, f"部门{did}")}
            for did in dept_ids
        ],
        "models": models,
    }
