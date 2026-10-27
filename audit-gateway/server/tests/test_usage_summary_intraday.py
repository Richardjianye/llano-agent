"""用量概览的 5 分钟趋势接口契约。"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from model.response import UserInfo
from services.permissions import USAGE_SCOPE_ALL, UsageScope
from services.usage_query import DailyRow


@pytest.mark.asyncio
async def test_single_day_summary_returns_288_five_minute_buckets(monkeypatch):
    import router.admin.usage as route

    async def scope_of(_user):
        return UsageScope(scope=USAGE_SCOPE_ALL)

    async def daily_rows(*_args, **_kwargs):
        return [DailyRow(
            stat_date=date(2026, 9, 11),
            user_uid="u1",
            dept_id=1,
            role="employee",
            model_id=10,
            model_name="demo",
            requests=1,
            success_count=1,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            latency_ms_sum=200,
        )]

    async def intraday_rows(*_args, **_kwargs):
        return [SimpleNamespace(
            created_at=datetime(2026, 9, 11, 10, 7, 33),
            model_id=10,
            billing_mode_snapshot="token",
            cost=0,
            status="success",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_prompt_tokens=0,
            tokens_estimated=False,
            latency_ms=200,
        )]

    async def prices():
        return {}

    async def active_users(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(route, "_scope_of", scope_of)
    monkeypatch.setattr(route, "fetch_daily_rows", daily_rows)
    monkeypatch.setattr(route, "fetch_intraday_records", intraday_rows)
    monkeypatch.setattr(route, "fetch_model_prices", prices)
    monkeypatch.setattr(route, "count_active_chat_users", active_users)

    response = await route.usage_summary(
        start=date(2026, 9, 11),
        end=date(2026, 9, 11),
        granularity="day",
        trend_interval="5m",
        dept_id=None,
        user_uid=None,
        role=None,
        model_id=None,
        user=UserInfo(id=1, uid="operator", username="admin", permission="president"),
    )

    assert response.data["active_users"] == 1
    assert len(response.data["trend"]) == 288
    point = next(item for item in response.data["trend"] if item["label"] == "10:05")
    assert point["metrics"]["total_tokens"] == 120


@pytest.mark.asyncio
async def test_five_minute_trend_rejects_multi_day_range(monkeypatch):
    import router.admin.usage as route
    from utils.errors import ValidationError_

    async def scope_of(_user):
        return UsageScope(scope=USAGE_SCOPE_ALL)

    async def daily_rows(*_args, **_kwargs):
        return []

    async def prices():
        return {}

    async def active_users(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(route, "_scope_of", scope_of)
    monkeypatch.setattr(route, "fetch_daily_rows", daily_rows)
    monkeypatch.setattr(route, "fetch_model_prices", prices)
    monkeypatch.setattr(route, "count_active_chat_users", active_users)

    with pytest.raises(ValidationError_):
        await route.usage_summary(
            start=date(2026, 9, 10),
            end=date(2026, 9, 11),
            granularity="day",
            trend_interval="5m",
            dept_id=None,
            user_uid=None,
            role=None,
            model_id=None,
            user=UserInfo(id=1, uid="operator", username="admin", permission="president"),
        )
