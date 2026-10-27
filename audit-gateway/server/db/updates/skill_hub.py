"""自有 skill hub 的上下架 —— 阶段 11 · S11-G

下架**不删数据**。已经装到员工机器上的那些不会因为下架而消失，留着这一行才能
回答「这个技能当时是谁发的、为什么被下掉」—— 而下架之后最常见的下一句话就是
员工来问「我搜不到 xxx 了」。
"""

from typing import Optional

from db import init_async_session
from model.database import SkillHubSkill, now
from services.skill_hub import DELISTED, LISTED
from sqlalchemy import select, update


async def set_skill_status(
    skill_id: int,
    *,
    status: str,
    operator_uid: str,
    reason: str = "",
) -> Optional[dict]:
    """上下架。返回改完之后的状态；技能不存在时返回 None。"""
    if status not in {LISTED, DELISTED}:
        raise ValueError(f"未知状态 {status!r}")

    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (
            await session.execute(select(SkillHubSkill).where(SkillHubSkill.id == skill_id))
        ).scalar_one_or_none()
        if skill is None:
            return None

        if status == DELISTED:
            values = {
                "status": DELISTED,
                "delisted_reason": (reason or "").strip()[:512] or None,
                "delisted_by_uid": operator_uid,
                "delisted_at": now(),
                "updated_at": now(),
            }
        else:
            # 恢复上架时把下架痕迹清掉。留着的话列表上会同时显示「已上架」和
            # 一条下架原因，看的人无从判断这是历史还是现状。
            values = {
                "status": LISTED,
                "delisted_reason": None,
                "delisted_by_uid": None,
                "delisted_at": None,
                "updated_at": now(),
            }

        await session.execute(
            update(SkillHubSkill).where(SkillHubSkill.id == skill_id).values(**values)
        )
        await session.commit()

        return {"id": skill_id, "slug": skill.slug, "status": status}


async def set_skill_scope(
    skill_id: int, *, scope: str, operator_uid: str, dept_id: Optional[int] = None
) -> Optional[dict]:
    """晋级到公司空间，或退回部门空间。

    退回时把 `promoted_*` 清掉：留着的话列表上会同时显示「部门空间」和一条晋级
    记录，看的人无从判断这是历史还是现状（与下架痕迹同一个处理）。

    退回的目标部门用**技能自己的 owner_dept_id**，不是操作人的部门 —— 操作人可能
    是管理部门的人，把技能退回到管理部门显然不对。
    """
    from model.database import SkillHubSkill
    from services.skill_hub import SCOPE_COMPANY, SCOPE_DEPT

    if scope not in {SCOPE_DEPT, SCOPE_COMPANY}:
        raise ValueError(f"未知空间 {scope!r}")

    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (
            await session.execute(select(SkillHubSkill).where(SkillHubSkill.id == skill_id))
        ).scalar_one_or_none()
        if skill is None:
            return None

        if scope == SCOPE_COMPANY:
            values = {
                "scope": SCOPE_COMPANY, "scope_dept_id": None,
                "promoted_by_uid": operator_uid, "promoted_at": now(), "updated_at": now(),
            }
        else:
            values = {
                "scope": SCOPE_DEPT,
                "scope_dept_id": dept_id if dept_id is not None else skill.owner_dept_id,
                "promoted_by_uid": None, "promoted_at": None, "updated_at": now(),
            }

        await session.execute(
            update(SkillHubSkill).where(SkillHubSkill.id == skill_id).values(**values)
        )
        await session.commit()
        return {"id": skill_id, "slug": skill.slug, "scope": scope,
                "scope_dept_id": values.get("scope_dept_id")}
