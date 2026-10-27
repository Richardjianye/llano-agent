"""模型日 token 配额。

额度按 usage_daily 中已经落账的 token 判断。一次请求的实际输出量只有上游完成后
才知道，所以当前请求允许有限度越过阈值；从下一次请求开始硬拒绝。这样不会偷偷
也不会用不可靠的字符估算提前扣减额度。

部门规则覆盖整棵下级树：子部门请求会同时命中本部门及所有上级部门规则，父部门
规则的已用量也汇总自己和所有后代。父子同时配置时两条都约束，任意一条耗尽即拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from db import init_async_session
from model.database import TokenQuota, UsageDaily
from services.org_tree import dept_ancestor_ids, dept_scope_ids
from sqlalchemy import delete, func, or_, select, update

QUOTA_SCOPE_USER = "user"
QUOTA_SCOPE_DEPT = "dept"
QUOTA_SCOPES = (QUOTA_SCOPE_USER, QUOTA_SCOPE_DEPT)


@dataclass(frozen=True)
class QuotaStatus:
    rule_id: int
    scope: str
    scope_value: str
    limit: int
    used: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exceeded(self) -> bool:
        return self.used >= self.limit


def first_blocking_quota(statuses: Iterable[QuotaStatus]) -> Optional[QuotaStatus]:
    """用户规则与部门规则同时生效；任意一条耗尽即拒绝。"""
    return next((status for status in statuses if status.exceeded), None)


async def list_quota_rules() -> list[TokenQuota]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return list((await session.execute(select(TokenQuota).order_by(TokenQuota.id))).scalars())


async def get_quota_rule(rule_id: int) -> Optional[TokenQuota]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return await session.get(TokenQuota, rule_id)


async def find_quota_rule(
    scope: str, scope_value: str, model_id: int
) -> Optional[TokenQuota]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(TokenQuota).where(
                    TokenQuota.scope == scope,
                    TokenQuota.scope_value == scope_value,
                    TokenQuota.model_id == model_id,
                )
            )
        ).scalar_one_or_none()


async def save_quota_rule(
    *, rule_id: Optional[int], scope: str, scope_value: str, model_id: int,
    daily_token_limit: int, status: int, remark: Optional[str],
) -> int:
    session_maker = await init_async_session()
    async with session_maker() as session:
        values = {
            "scope": scope,
            "scope_value": scope_value,
            "model_id": model_id,
            "daily_token_limit": daily_token_limit,
            "status": status,
            "remark": remark,
        }
        if rule_id is None:
            record = TokenQuota(**values)
            session.add(record)
            await session.flush()
            saved_id = record.id
        else:
            await session.execute(update(TokenQuota).where(TokenQuota.id == rule_id).values(values))
            saved_id = rule_id
        await session.commit()
        return int(saved_id)


async def delete_quota_rule(rule_id: int) -> None:
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(delete(TokenQuota).where(TokenQuota.id == rule_id))
        await session.commit()


async def _used_tokens(*, model_id: int, user_uid: Optional[str] = None,
                       dept_ids: Optional[Iterable[int]] = None,
                       stat_date: Optional[date] = None) -> int:
    stmt = select(func.coalesce(func.sum(UsageDaily.total_tokens), 0)).where(
        UsageDaily.stat_date == (stat_date or date.today()),
        UsageDaily.model_id == model_id,
    )
    if user_uid is not None:
        stmt = stmt.where(UsageDaily.user_uid == user_uid)
    if dept_ids is not None:
        stmt = stmt.where(UsageDaily.dept_id.in_(tuple(int(i) for i in dept_ids)))
    session_maker = await init_async_session()
    async with session_maker() as session:
        return int((await session.execute(stmt)).scalar_one())


async def quota_status_for_rule(rule: TokenQuota) -> QuotaStatus:
    dept_ids = None
    if rule.scope == QUOTA_SCOPE_DEPT:
        dept_ids = await dept_scope_ids(int(rule.scope_value))

    used = await _used_tokens(
        model_id=rule.model_id,
        user_uid=rule.scope_value if rule.scope == QUOTA_SCOPE_USER else None,
        dept_ids=dept_ids,
    )
    return QuotaStatus(rule.id, rule.scope, rule.scope_value, int(rule.daily_token_limit), used)


async def applicable_quota_statuses(
    uid: str, dept_id: Optional[int], model_id: int
) -> list[QuotaStatus]:
    predicates = [
        (TokenQuota.scope == QUOTA_SCOPE_USER) & (TokenQuota.scope_value == uid),
    ]
    ancestor_ids = await dept_ancestor_ids(dept_id)
    if ancestor_ids:
        predicates.append(
            (TokenQuota.scope == QUOTA_SCOPE_DEPT)
            & (TokenQuota.scope_value.in_(tuple(str(i) for i in ancestor_ids)))
        )

    session_maker = await init_async_session()
    async with session_maker() as session:
        rules = list((await session.execute(select(TokenQuota).where(
            TokenQuota.model_id == model_id,
            TokenQuota.status == 1,
            or_(*predicates),
        ))).scalars())

    statuses = []
    for rule in rules:
        statuses.append(await quota_status_for_rule(rule))
    return statuses
