"""单价快照的纯逻辑测试 —— 「调价之后历史金额还对不对」

这组测试盯的是一类**不会报错、只会让金额悄悄变掉**的问题：

    厂商 9 月降价一半 → 后台照新价目表改了单价
    → 8 月那份已经发出去的报表，今天再打开金额少一半
    → 没人说得清哪个数是对的，与厂商账单更是永远对不上

所以核心断言只有一句：**快照存在时，改当前单价不能影响历史金额。**
围绕它的其余用例都在守「算不出时必须报 None 而不是 0」这条老规矩
（决策 #22）——按 0 计入的总额偏小，而偏小的数字看起来完全正常。
"""

from datetime import date
from decimal import Decimal

from services.usage import UsageFacts, build_price_snapshot
from services.usage_query import (BILLING_PER_CALL, BILLING_TOKEN,
                                  COST_BASIS_CURRENT, COST_BASIS_MIXED,
                                  COST_BASIS_SNAPSHOT, DailyRow, Metrics,
                                  ModelPrice, cost_basis_note, row_cost,
                                  row_metrics, total_metrics)

# 元 / 百万 token（决策 #25）。别改成 0.001 那种「千 token」的旧值：
# 单位错一个数量级时金额看起来仍然像个正常数字，最难发现
PRICE = ModelPrice(input_per_1m=Decimal("1"), output_per_1m=Decimal("2"))

# 同一个模型「后来涨了 10 倍」，用来验证历史金额不跟着动
PRICE_AFTER_HIKE = ModelPrice(
    input_per_1m=Decimal("10"), output_per_1m=Decimal("20")
)


def make_facts(**kwargs) -> UsageFacts:
    base = dict(
        request_id="rid-1",
        user_uid="u1",
        model_id=10,
        model_name="deepseek-chat",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        price=PRICE,
    )
    base.update(kwargs)
    return UsageFacts(**base)


def make_row(**kwargs) -> DailyRow:
    """一行日聚合。默认是「全部请求都带快照且都算得出费用」"""
    base = dict(
        stat_date=date(2026, 8, 5),
        user_uid="u1",
        dept_id=1,
        role="employee",
        model_id=10,
        model_name="deepseek-chat",
        requests=2,
        success_count=2,
        prompt_tokens=2000,
        completion_tokens=1000,
        total_tokens=3000,
        cost_sum=Decimal("0.004"),
        snapshot_requests=2,
        priced_requests=2,
    )
    base.update(kwargs)
    return DailyRow(**base)


# ==================== 写入侧：这一笔按什么价算的 ====================

def test_按token计价当场算出费用():
    snapshot = build_price_snapshot(make_facts())

    # 1000 输入 × 1元/M + 500 输出 × 2元/M = 0.001 + 0.001
    assert snapshot.cost == Decimal("0.002000")
    assert snapshot.priced is True


def test_单价一并落库以便事后解释():
    """光存费用不存单价，日后「这个数怎么来的」就没法回答"""
    columns = build_price_snapshot(make_facts()).columns

    assert columns["billing_mode_snapshot"] == BILLING_TOKEN
    assert columns["input_price_per_1m_snapshot"] == Decimal("1")
    assert columns["output_price_per_1m_snapshot"] == Decimal("2")
    assert columns["cost"] == Decimal("0.002000")
    assert columns["cost_currency"] == "CNY"


def test_未配单价时费用为None而不是0():
    snapshot = build_price_snapshot(make_facts(price=ModelPrice()))

    assert snapshot.cost is None
    assert snapshot.priced is False


def test_未配单价也要记下当时的计价模式():
    """记下来才解释得了「为什么这笔算不出」——是没配价，还是压根没路由到模型"""
    price = ModelPrice(billing_mode=BILLING_PER_CALL, price_per_call=None)
    columns = build_price_snapshot(make_facts(price=price)).columns

    assert columns["billing_mode_snapshot"] == BILLING_PER_CALL
    assert columns["cost"] is None


def test_没有价格对象时全部留空():
    columns = build_price_snapshot(make_facts(price=None)).columns

    assert columns["cost"] is None
    assert columns["billing_mode_snapshot"] is None


def test_缓存单价留空就原样留空():
    """留空的含义是「当时没单独配，按输入价计」。

    在快照里替换成输入单价会把「没配」和「配成和输入一样」抹平，
    日后想查「那会儿到底配没配缓存价」就查不出来了。
    """
    columns = build_price_snapshot(make_facts()).columns

    assert columns["cached_input_price_per_1m_snapshot"] is None


def test_按次计价的失败调用不收钱():
    """不特判的话，上游 502 一次也要收一次的钱 ——
    月底报表里会凭空多出一批「花了钱什么都没拿到」的调用"""
    price = ModelPrice(
        billing_mode=BILLING_PER_CALL, price_per_call=Decimal("0.05")
    )
    snapshot = build_price_snapshot(
        make_facts(price=price, status="failed", prompt_tokens=0,
                   completion_tokens=0, total_tokens=0)
    )

    assert snapshot.cost == Decimal("0")
    # 仍算「已定价」：算出来是 0 和算不出来是两回事
    assert snapshot.priced is True


def test_按次计价的成功调用收一次():
    price = ModelPrice(
        billing_mode=BILLING_PER_CALL, price_per_call=Decimal("0.05")
    )
    assert build_price_snapshot(make_facts(price=price)).cost == Decimal("0.050000")


def test_中断的调用照常计费():
    """中断是**成功**的调用：token 已消耗、内容已产出，只是没人看完（决策 #28）"""
    snapshot = build_price_snapshot(make_facts(aborted=True, status="success"))

    assert snapshot.cost == Decimal("0.002000")


def test_费用量化到六位小数():
    """库里是 DECIMAL(18,6)，先量化到同一精度，
    免得「库里存的」和「刚算出来的」差在第 7 位上，白白多一层对账疑问"""
    price = ModelPrice(
        input_per_1m=Decimal("0.5"), output_per_1m=Decimal("0")
    )
    # 1 token × 0.5元/百万 = 5e-7，四舍五入到 1e-6
    snapshot = build_price_snapshot(
        make_facts(price=price, prompt_tokens=1, completion_tokens=0)
    )

    assert snapshot.cost == Decimal("0.000001")


def test_from_model读取模型行的单价():
    class FakeModel:
        billing_mode = "token"
        input_price_per_1m = Decimal("3")
        output_price_per_1m = Decimal("6")
        cached_input_price_per_1m = None
        price_per_call = None
        currency = "CNY"

    price = ModelPrice.from_model(FakeModel())

    assert price.input_per_1m == Decimal("3")
    assert price.configured is True


def test_from_model容忍空对象():
    assert ModelPrice.from_model(None).configured is False


# ==================== 读取侧：报表该用哪个价 ====================

def test_有快照时调价不影响历史金额():
    """这一条是整个功能的理由：改单价不该让上个月的报表变数"""
    row = make_row()

    before = row_cost(row, {10: PRICE})
    after = row_cost(row, {10: PRICE_AFTER_HIKE})

    assert before == after == (Decimal("0.004"), True)


def test_没有快照的历史行按当前单价回算():
    """快照上线前的数据两个计数器都是 0，走回算分支 —— 与上线前行为一致"""
    row = make_row(cost_sum=Decimal("0"), snapshot_requests=0, priced_requests=0)

    cost, from_snapshot = row_cost(row, {10: PRICE})

    # 2000 输入 × 1元/M + 1000 输出 × 2元/M = 0.002 + 0.002
    assert cost == Decimal("0.004")
    assert from_snapshot is False


def test_快照残缺的行整行回算():
    """跨越上线当天的那一行：cost_sum 只覆盖了后半天，
    直接用会漏掉前半天的笔数而金额偏小。整行回算才是诚实的做法"""
    row = make_row(snapshot_requests=1, cost_sum=Decimal("0.002"))

    cost, from_snapshot = row_cost(row, {10: PRICE})

    assert cost == Decimal("0.004")
    assert from_snapshot is False


def test_有笔调用当时没配单价则整行算不出():
    """把配了价的那部分加起来，会给出一个偏小却看起来很正常的数字"""
    row = make_row(priced_requests=1)

    cost, from_snapshot = row_cost(row, {10: PRICE})

    assert cost is None
    assert from_snapshot is True


def test_快照行的口径标记为snapshot():
    metrics = row_metrics(make_row(), {10: PRICE})

    assert metrics.cost_basis == COST_BASIS_SNAPSHOT
    assert metrics.to_dict()["cost"] == 0.004


def test_回算行的口径标记为current():
    row = make_row(snapshot_requests=0, priced_requests=0, cost_sum=Decimal("0"))
    metrics = row_metrics(row, {10: PRICE})

    assert metrics.cost_basis == COST_BASIS_CURRENT
    assert metrics.to_dict()["cost_basis"] == COST_BASIS_CURRENT


def test_两种混在一组时标记为mixed():
    """跨越上线那天的区间必然如此，不标出来就没人知道这个总额是拼出来的"""
    rows = [
        make_row(),
        make_row(snapshot_requests=0, priced_requests=0, cost_sum=Decimal("0")),
    ]
    total = total_metrics(rows, {10: PRICE})

    assert total.cost_basis == COST_BASIS_MIXED
    assert total.cost == Decimal("0.008")


def test_空指标不谎称有历史数据():
    """空组金额是 0，说它「按当前单价回算」会让人以为有历史数据要担心"""
    assert Metrics().cost_basis == COST_BASIS_SNAPSHOT


def test_口径提示只在确实混了回算时才出现():
    assert cost_basis_note(row_metrics(make_row(), {10: PRICE})) == ""

    mixed = total_metrics(
        [make_row(), make_row(snapshot_requests=0, priced_requests=0)],
        {10: PRICE},
    )
    note = cost_basis_note(mixed)
    assert "按当前单价回算" in note
