"""用量报表读取侧的纯逻辑测试

重点覆盖三件最容易悄悄算错、且错了没人发现的事：

1. **时间分桶**：跨月、跨年、周一边界
2. **估算与实测分开**：混在一起会让人以为数字很精确
3. **费用算不全时必须是 None 而不是 0**：按 0 计入总额会让总数偏小，
   而偏小的总数看起来完全正常，是最危险的那种 bug
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from services.usage_query import (GRANULARITY_DAY, GRANULARITY_MONTH,
                                  GRANULARITY_WEEK, MAX_RANGE_DAYS, DailyRow,
                                  Metrics, ModelPrice, UsageQueryError,
                                  aggregate, bucket_label, bucket_of,
                                  compute_cost, fill_buckets,
                                  fill_five_minute_buckets, five_minute_bucket,
                                  quantize_money,
                                  resolve_range, sort_groups, to_csv,
                                  total_metrics, week_start)


def make_row(**kwargs) -> DailyRow:
    """构造一行日聚合，只覆盖用得上的字段"""
    base = dict(
        stat_date=date(2026, 8, 5),
        user_uid="u1",
        dept_id=1,
        role="employee",
        model_id=10,
        model_name="deepseek-chat",
        requests=1,
        success_count=1,
        failed_count=0,
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        latency_ms_sum=200,
        estimated_requests=0,
        estimated_tokens=0,
    )
    base.update(kwargs)
    return DailyRow(**base)


PRICES = {
    # 单价口径是**元 / 百万 token**（跟厂商价目表一致）。
    # 这两个数字接近 DeepSeek 的真实量级，别改成 0.001 那种「千 token」的旧值 ——
    # 单位错一个数量级时金额看起来仍然像个正常数字，最难发现
    10: ModelPrice(input_per_1m=Decimal("1"), output_per_1m=Decimal("2")),
    # 11 号模型故意不配单价，用来验证「算不出来就别瞎报数」
    11: ModelPrice(),
}


# ==================== 时间分桶 ====================

def test_bucket_day():
    assert bucket_of(date(2026, 8, 5), GRANULARITY_DAY) == "2026-08-05"


def test_bucket_week_starts_on_monday():
    """2026-08-05 是周三，应归到当周周一 08-03"""
    assert bucket_of(date(2026, 8, 5), GRANULARITY_WEEK) == "2026-08-03"
    assert bucket_of(date(2026, 8, 3), GRANULARITY_WEEK) == "2026-08-03"
    assert bucket_of(date(2026, 8, 9), GRANULARITY_WEEK) == "2026-08-03"
    # 周日的下一天进入新桶
    assert bucket_of(date(2026, 8, 10), GRANULARITY_WEEK) == "2026-08-10"


def test_bucket_week_across_month_boundary():
    """跨月的那一周不能被月份切成两半"""
    assert bucket_of(date(2026, 9, 1), GRANULARITY_WEEK) == "2026-08-31"


def test_bucket_month():
    assert bucket_of(date(2026, 8, 31), GRANULARITY_MONTH) == "2026-08"
    assert bucket_of(date(2026, 1, 1), GRANULARITY_MONTH) == "2026-01"


def test_bucket_keys_sort_chronologically():
    """桶 key 直接拿来排序，所以必须补零成定长串"""
    keys = [bucket_of(date(2026, m, 1), GRANULARITY_MONTH) for m in (12, 1, 9)]
    assert sorted(keys) == ["2026-01", "2026-09", "2026-12"]


def test_week_label_shows_range():
    assert bucket_label("2026-08-03", GRANULARITY_WEEK) == "2026-08-03 ~ 2026-08-09"


def test_week_start_of_monday_is_itself():
    assert week_start(date(2026, 8, 3)) == date(2026, 8, 3)


# ==================== 区间解析 ====================

def test_default_day_range_is_today():
    start, end = resolve_range(None, None, GRANULARITY_DAY, today=date(2026, 8, 7))
    assert (start, end) == (date(2026, 8, 7), date(2026, 8, 7))


def test_default_week_range_is_current_natural_week():
    start, end = resolve_range(None, None, GRANULARITY_WEEK, today=date(2026, 8, 7))
    assert (start, end) == (date(2026, 8, 3), date(2026, 8, 9))


def test_default_month_range_is_current_calendar_month():
    start, end = resolve_range(None, None, GRANULARITY_MONTH, today=date(2026, 8, 7))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 31)


def test_reversed_range_is_rejected():
    with pytest.raises(UsageQueryError):
        resolve_range(date(2026, 8, 7), date(2026, 8, 1), GRANULARITY_DAY)


def test_oversized_range_is_rejected():
    """跨度上限是为了保护数据库，不能悄悄放行"""
    with pytest.raises(UsageQueryError):
        resolve_range(date(2020, 1, 1), date(2026, 1, 1), GRANULARITY_DAY)


def test_range_at_exactly_the_limit_is_allowed():
    from datetime import timedelta
    end = date(2026, 8, 7)
    start = end - timedelta(days=MAX_RANGE_DAYS - 1)
    assert resolve_range(start, end, GRANULARITY_DAY) == (start, end)


def test_invalid_granularity_is_rejected():
    with pytest.raises(UsageQueryError):
        resolve_range(None, None, "quarter")


def test_fill_buckets_includes_empty_days():
    """趋势图缺口要显式补 0，不能靠「没数据就不画点」"""
    buckets = fill_buckets(date(2026, 8, 1), date(2026, 8, 3), GRANULARITY_DAY)
    assert buckets == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_fill_buckets_dedupes_within_a_month():
    buckets = fill_buckets(date(2026, 8, 1), date(2026, 9, 15), GRANULARITY_MONTH)
    assert buckets == ["2026-08", "2026-09"]


def test_five_minute_buckets_cover_a_whole_day():
    buckets = fill_five_minute_buckets(date(2026, 8, 7))
    assert len(buckets) == 288
    assert buckets[0] == "2026-08-07T00:00:00"
    assert buckets[-1] == "2026-08-07T23:55:00"
    assert five_minute_bucket(datetime(2026, 8, 7, 9, 17, 42)) == "2026-08-07T09:15:00"


# ==================== 费用估算 ====================

def test_compute_cost():
    """1000 输入 × 1 元/M + 500 输出 × 2 元/M = 0.001 + 0.001 = 0.002"""
    cost = compute_cost(1000, 500, PRICES[10])
    assert cost == Decimal("0.002")


def test_price_unit_is_per_million():
    """单价口径钉死在「每百万 token」

    这条断言存在的唯一理由是防止有人把除数改回 1000。
    单位错一个数量级不会让任何测试崩，只会让报表金额悄悄差 1000 倍，
    而那个数字看起来仍然完全正常。
    """
    price = ModelPrice(input_per_1m=Decimal("2"), output_per_1m=Decimal("8"))
    # 正好一百万输入 token = 一份输入单价
    assert compute_cost(1_000_000, 0, price) == Decimal("2")
    assert compute_cost(0, 1_000_000, price) == Decimal("8")


def test_cost_is_none_when_price_missing():
    """未配置单价 ≠ 免费。返回 0 会让总额悄悄偏小，这是最难发现的错"""
    assert compute_cost(1000, 500, PRICES[11]) is None
    assert compute_cost(1000, 500, None) is None


def test_cost_is_none_when_only_one_side_configured():
    half = ModelPrice(input_per_1m=Decimal("1"))
    assert compute_cost(1000, 500, half) is None


def test_money_uses_decimal_not_float():
    """float 累加会出现 0.1+0.2 那种误差，对账时排查成本极高"""
    price = ModelPrice(input_per_1m=Decimal("100"), output_per_1m=Decimal("200"))
    total = sum((compute_cost(1000, 1000, price) for _ in range(10)), Decimal("0"))
    assert total == Decimal("3.0")


def test_quantize_keeps_four_decimals():
    assert quantize_money(Decimal("0.00005")) == Decimal("0.0001")
    assert quantize_money(Decimal("1.234567")) == Decimal("1.2346")


# ==================== 聚合 ====================

def test_aggregate_by_user():
    rows = [
        make_row(user_uid="u1", total_tokens=100),
        make_row(user_uid="u1", total_tokens=200),
        make_row(user_uid="u2", total_tokens=50),
    ]
    result = aggregate(rows, "user", GRANULARITY_DAY, PRICES)
    assert result["u1"].total_tokens == 300
    assert result["u2"].total_tokens == 50
    assert result["u1"].requests == 2


def test_aggregate_by_date_respects_granularity():
    rows = [
        make_row(stat_date=date(2026, 8, 3), total_tokens=10),
        make_row(stat_date=date(2026, 8, 5), total_tokens=20),
        make_row(stat_date=date(2026, 8, 12), total_tokens=30),
    ]
    weekly = aggregate(rows, "date", GRANULARITY_WEEK, PRICES)
    assert weekly["2026-08-03"].total_tokens == 30
    assert weekly["2026-08-10"].total_tokens == 30


def test_null_dept_groups_into_empty_key():
    """没有部门的账号要单独成组，不能在聚合时被丢掉"""
    rows = [make_row(dept_id=None), make_row(dept_id=2)]
    result = aggregate(rows, "dept", GRANULARITY_DAY, PRICES)
    assert "" in result
    assert "2" in result


def test_unknown_dimension_is_rejected():
    with pytest.raises(UsageQueryError):
        aggregate([make_row()], "planet", GRANULARITY_DAY, PRICES)


def test_estimated_tokens_tracked_separately():
    """估算与实测必须分开，否则报表数字看起来比实际精确"""
    rows = [
        make_row(total_tokens=100, estimated_requests=0, estimated_tokens=0),
        make_row(total_tokens=300, estimated_requests=1, estimated_tokens=300),
    ]
    total = total_metrics(rows, PRICES)
    assert total.total_tokens == 400
    assert total.estimated_tokens == 300
    assert total.estimated_requests == 1


def test_one_unpriced_model_poisons_the_whole_group():
    """一组里只要有一个模型没配单价，整组费用就是「算不全」而不是部分和"""
    rows = [make_row(model_id=10), make_row(model_id=11)]
    total = total_metrics(rows, PRICES)
    assert total.cost_complete is False
    assert total.to_dict()["cost"] is None


def test_priced_group_reports_a_number():
    total = total_metrics([make_row(model_id=10)], PRICES)
    assert total.to_dict()["cost"] == pytest.approx(0.002)


def test_avg_latency_is_zero_without_requests():
    """除零保护 —— 空分组在补 0 的趋势图里天天出现"""
    assert Metrics().avg_latency_ms == 0


def test_avg_latency_rounds():
    rows = [make_row(latency_ms_sum=100), make_row(latency_ms_sum=201)]
    assert total_metrics(rows, PRICES).avg_latency_ms == 151


# ==================== 排序 ====================

def test_groups_sorted_by_tokens_desc():
    rows = [
        make_row(user_uid="small", total_tokens=1),
        make_row(user_uid="big", total_tokens=999),
    ]
    ordered = sort_groups(aggregate(rows, "user", GRANULARITY_DAY, PRICES), "user")
    assert [k for k, _ in ordered] == ["big", "small"]


def test_date_groups_sorted_chronologically_not_by_size():
    rows = [
        make_row(stat_date=date(2026, 8, 5), total_tokens=1),
        make_row(stat_date=date(2026, 8, 1), total_tokens=999),
    ]
    ordered = sort_groups(aggregate(rows, "date", GRANULARITY_DAY, PRICES), "date")
    assert [k for k, _ in ordered] == ["2026-08-01", "2026-08-05"]


def test_limit_truncates_after_sorting():
    rows = [make_row(user_uid=f"u{i}", total_tokens=i) for i in range(10)]
    ordered = sort_groups(
        aggregate(rows, "user", GRANULARITY_DAY, PRICES), "user", limit=3
    )
    assert [k for k, _ in ordered] == ["u9", "u8", "u7"]


# ==================== 导出 ====================

def test_csv_has_header_and_rows():
    total = total_metrics([make_row()], PRICES)
    csv_text = to_csv([("admin", total)])
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("分组,")
    assert "admin" in lines[1]


def test_csv_leaves_cost_blank_when_incomplete():
    """导出的表格同样不能骗人：算不出来就留空，不是 0"""
    total = total_metrics([make_row(model_id=11)], PRICES)
    csv_text = to_csv([("某人", total)])
    row = csv_text.strip().splitlines()[1]
    # 倒数第二列是费用，应为空
    assert row.split(",")[-2] == ""


def test_csv_appends_note():
    csv_text = to_csv([("x", Metrics())], note="数据来自上游")
    assert "数据来自上游" in csv_text


# ==================== 计价模式（决策 #27）====================
#
# 这一组盯着两类「不会报错、只会让金额悄悄错掉」的问题：
#
# 1. 缓存那部分被重复计价 —— 上游返回的 prompt_tokens 是**含**缓存命中的总数，
#    不减出来就等于按全价又算了一遍
# 2. 「没配缓存单价」被当成「缓存免费」—— 费用凭空偏低，而偏低的数字看着很正常


def test_缓存命中按缓存单价计费():
    from services.usage_query import BILLING_TOKEN

    price = ModelPrice(
        input_per_1m=Decimal("10"),
        output_per_1m=Decimal("20"),
        cached_input_per_1m=Decimal("1"),   # 缓存便宜一个数量级
        billing_mode=BILLING_TOKEN,
    )
    # 100 万输入里有 80 万命中缓存
    cost = compute_cost(1_000_000, 0, price, cached_prompt_tokens=800_000)

    # 20万 × 10元/M + 80万 × 1元/M = 2 + 0.8 = 2.8
    assert cost == Decimal("2.8")


def test_缓存部分不能被重复计价():
    """prompt_tokens 含缓存命中，减不干净就会按全价多算一遍"""
    price = ModelPrice(
        input_per_1m=Decimal("10"),
        output_per_1m=Decimal("0"),
        cached_input_per_1m=Decimal("0"),   # 缓存当免费，方便凸显差额
    )
    full = compute_cost(1_000_000, 0, price, cached_prompt_tokens=0)
    all_cached = compute_cost(1_000_000, 0, price, cached_prompt_tokens=1_000_000)

    assert full == Decimal("10")
    assert all_cached == Decimal("0"), "全部命中缓存却仍在收费，说明没把缓存减出来"


def test_未配缓存单价时按输入单价而不是免费():
    """留空 =「没单独配」，不是「缓存不要钱」

    按 0 算会让费用凭空偏低 —— 而偏低的金额看起来完全正常，最难发现。
    """
    price = ModelPrice(input_per_1m=Decimal("10"), output_per_1m=Decimal("0"))
    cost = compute_cost(1_000_000, 0, price, cached_prompt_tokens=500_000)

    assert cost == Decimal("10"), "未配缓存单价时应全按输入单价，不能白送"


def test_缓存数超过输入数时不产生负费用():
    """上游把 cached 报得比 prompt 还大是真实见过的脏数据，
    直接相减会得到负数，让整份报表金额莫名变小"""
    price = ModelPrice(input_per_1m=Decimal("10"), output_per_1m=Decimal("0"),
                       cached_input_per_1m=Decimal("1"))
    cost = compute_cost(1000, 0, price, cached_prompt_tokens=999_999)

    assert cost is not None and cost >= 0


def test_按次计价与token数无关():
    from services.usage_query import BILLING_PER_CALL

    price = ModelPrice(
        billing_mode=BILLING_PER_CALL,
        price_per_call=Decimal("0.05"),
        # 故意不配 token 单价：按次模式下它们不该被要求
        input_per_1m=None,
        output_per_1m=None,
    )
    assert compute_cost(999_999, 999_999, price, requests=3) == Decimal("0.15")
    assert compute_cost(0, 0, price, requests=3) == Decimal("0.15")


def test_按次模式缺单价时算不出费用():
    from services.usage_query import BILLING_PER_CALL

    price = ModelPrice(billing_mode=BILLING_PER_CALL, price_per_call=None)
    assert compute_cost(1000, 500, price, requests=1) is None


def test_按次模式不该被token单价卡住():
    """别拿 token 模式的「必须两个单价都填」去卡按次模式"""
    from services.usage_query import BILLING_PER_CALL

    price = ModelPrice(billing_mode=BILLING_PER_CALL, price_per_call=Decimal("0.01"))
    assert price.configured is True
