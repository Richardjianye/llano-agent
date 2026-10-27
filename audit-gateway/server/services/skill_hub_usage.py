"""调用事件按认证身份和事件 ID 去重，重试不会把调用量翻倍。"""
from db import init_async_session
from model.database import SkillHubUsageEvent
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.errors import ValidationError_


async def record_events(uid, api_key_id, events):
    maker = await init_async_session()
    accepted = []
    async with maker() as session:
        for event in events:
            previous = (await session.execute(select(SkillHubUsageEvent).where(
                SkillHubUsageEvent.user_uid == uid, SkillHubUsageEvent.event_id == event.event_id))).scalar_one_or_none()
            if previous:
                if previous.slug != event.slug:
                    raise ValidationError_("同一调用事件不能变更技能标识")
                accepted.append(event.event_id)
                continue
            try:
                async with session.begin_nested():
                    session.add(SkillHubUsageEvent(user_uid=uid, api_key_id=api_key_id,
                        event_id=event.event_id, slug=event.slug))
                    await session.flush()
            except IntegrityError:
                # 并发重试撞唯一键。外层事务保持可用，再查原事件核对技能。
                previous = (await session.execute(select(SkillHubUsageEvent).where(
                    SkillHubUsageEvent.user_uid == uid, SkillHubUsageEvent.event_id == event.event_id))).scalar_one()
                if previous.slug != event.slug:
                    raise ValidationError_("同一调用事件不能变更技能标识")
            accepted.append(event.event_id)
        await session.commit()
    return accepted
