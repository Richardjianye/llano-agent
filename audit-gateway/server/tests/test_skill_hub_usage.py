from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from model.database import Base, SkillHubUsageEvent
from services.skill_hub import can_browse_all, can_promote
from services import skill_hub_usage


@pytest.mark.parametrize("role,dept,expected", [
    ("manager", "finance", True), ("auditor", None, True), ("developer", "rd", True),
    ("president", None, True), ("employee", "exec", True), ("employee", "digital-ops", True),
    ("employee", "rd", False), ("employee", None, False), ("unknown", None, False),
])
def test_all_skill_visibility_has_exactly_the_requested_subjects(role, dept, expected):
    assert can_browse_all(role, dept) is expected


def test_browsing_all_does_not_grant_cross_department_write_permissions():
    assert can_browse_all("manager", "finance")
    assert not can_promote("manager", 1, 2)


@pytest.mark.asyncio
async def test_real_database_deduplicates_retry_and_separates_users(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[SkillHubUsageEvent.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async def factory(): return maker
    monkeypatch.setattr(skill_hub_usage, "init_async_session", factory)
    events = [SimpleNamespace(event_id="a" * 32, slug="report")]
    try:
        await skill_hub_usage.record_events("user-a", 1, events)
        await skill_hub_usage.record_events("user-a", 2, events)  # 设备 Key 轮换也不重复。
        await skill_hub_usage.record_events("user-b", 3, events)
        async with maker() as session:
            assert await session.scalar(select(func.count(SkillHubUsageEvent.id))) == 2
    finally:
        await engine.dispose()
