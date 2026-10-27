"""用量统计的读取侧 —— 纯逻辑部分

写入侧在 services/usage.py，这里只负责「怎么把已有的数据切出来给人看」。
放在 services 层而不是路由里，是为了这些规则能脱离 MySQL 单测
（时间范围、分桶、费用估算全是容易算错又不容易发现的地方）。

三条必须守住的口径，报表出错基本都出在这三处：

1. **估算与实测分开**：上游没返回 usage 时 token 是按字符猜的，
   混在一起算总量会让人以为数字很精确。这里始终把 estimated_* 单独带出。
2. **费用是「估算」且可能算不出**：模型没配单价时费用为 None，
   不能悄悄按 0 计入总额，否则总额会偏小而没人察觉。
3. **日期是服务器本地日**：写入侧用 date.today() 落 stat_date，
   查询侧必须用同一个时区口径，否则跨零点的数据会对不上。

第四条是后加的，但同样属于「算错了没人发现」那一类：

4. **费用优先用调用当时的单价快照**，只有快照缺失才退回按当前单价回算，
   并且必须把用的是哪一种（cost_basis）如实带出去。拿今天的单价乘历史 token
   会让一份已经发出去的报表在厂商调价后金额自己变掉 —— 数字仍然「看起来正常」，
   却和当初那份、和厂商账单都对不上，是最难解释的一类差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Tuple

# ==================== 时间粒度 ====================

GRANULARITY_DAY = "day"
GRANULARITY_WEEK = "week"
GRANULARITY_MONTH = "month"

ALL_GRANULARITIES = (GRANULARITY_DAY, GRANULARITY_WEEK, GRANULARITY_MONTH)

# 一次查询最多跨多少天。日聚合表一天一用户一模型一行，跨度太大依然会拖慢页面，
# 上限比「查出来但很慢」更容易被发现，也更容易解释。
MAX_RANGE_DAYS = 366

# ==================== 下钻维度 ====================

DIMENSION_DATE = "date"
DIMENSION_USER = "user"
DIMENSION_DEPT = "dept"
DIMENSION_ROLE = "role"
DIMENSION_MODEL = "model"

ALL_DIMENSIONS = (
    DIMENSION_DATE,
    DIMENSION_USER,
    DIMENSION_DEPT,
    DIMENSION_ROLE,
    DIMENSION_MODEL,
)


class UsageQueryError(ValueError):
    """查询参数不合法 —— 由路由层转成 400"""


# ==================== 时间范围 ====================


def resolve_range(
    start: Optional[date],
    end: Optional[date],
    granularity: str,
    today: Optional[date] = None,
) -> Tuple[date, date]:
    """解析查询区间，两端都是闭区间

    没传日期时使用当前自然周期：日→今天、周→本周、月→本月。
    """
    if granularity not in ALL_GRANULARITIES:
        raise UsageQueryError(f"时间粒度非法，可选值：{', '.join(ALL_GRANULARITIES)}")

    today = today or date.today()
    if start is None and end is None:
        if granularity == GRANULARITY_DAY:
            start = end = today
        elif granularity == GRANULARITY_WEEK:
            start = week_start(today)
            end = start + timedelta(days=6)
        else:
            start = month_start(today)
            end = date(today.year, today.month, monthrange(today.year, today.month)[1])
    else:
        end = end or today
        start = start or end

    if start > end:
        raise UsageQueryError("开始日期不能晚于结束日期")

    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise UsageQueryError(f"查询跨度不能超过 {MAX_RANGE_DAYS} 天，请缩小范围或分批导出")

    return start, end


def week_start(day: date) -> date:
    """所在周的周一 —— 国内习惯周一为一周之始，别用 ISO 之外的算法混着来"""
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    return day.replace(day=1)


def bucket_of(day: date, granularity: str) -> str:
    """把某天归到时间桶。桶 key 同时用作排序键，所以必须是可字典序比较的定长串"""
    if granularity == GRANULARITY_DAY:
        return day.isoformat()
    if granularity == GRANULARITY_WEEK:
        return week_start(day).isoformat()
    return f"{day.year:04d}-{day.month:02d}"


def bucket_label(bucket: str, granularity: str) -> str:
    """给人看的桶名"""
    if granularity == GRANULARITY_DAY:
        return bucket
    if granularity == GRANULARITY_WEEK:
        start = date.fromisoformat(bucket)
        return f"{bucket} ~ {(start + timedelta(days=6)).isoformat()}"
    return bucket


def five_minute_bucket(moment: datetime) -> str:
    """把服务器本地时间归到 5 分钟桶，key 可直接按时间排序。"""
    minute = moment.minute - moment.minute % 5
    return moment.replace(minute=minute, second=0, microsecond=0).isoformat()


def fill_five_minute_buckets(day: date) -> List[str]:
    """返回一天完整的 288 个五分钟桶，空桶由路由补零。"""
    start = datetime.combine(day, datetime.min.time())
    return [five_minute_bucket(start + timedelta(minutes=5 * i)) for i in range(288)]


# ==================== 指标聚合 ====================


# ==================== 费用口径 ====================
#
# 同一份报表里可能混着两种算法，必须让看的人能分辨：
#   snapshot —— 用调用当时记下的单价（金额不随日后调价变动）
#   current  —— 单价快照上线前的历史数据，只能拿当前单价回算
#   mixed    —— 一个分组里两种都有，跨越上线那天时会出现
COST_BASIS_SNAPSHOT = "snapshot"
COST_BASIS_CURRENT = "current"
COST_BASIS_MIXED = "mixed"

COST_BASIS_LABELS = {
    COST_BASIS_SNAPSHOT: "按调用时单价",
    COST_BASIS_CURRENT: "按当前单价回算",
    COST_BASIS_MIXED: "部分按当前单价回算",
}


@dataclass
class Metrics:
    """一组指标的累加器

    cost 单独用 Decimal 累加：金额不能用 float 累加，
    几万行下来的误差虽小，但对不上账时排查成本极高。
    """

    requests: int = 0
    success_count: int = 0
    failed_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_requests: int = 0
    estimated_tokens: int = 0
    latency_ms_sum: int = 0
    cost: Decimal = field(default_factory=lambda: Decimal("0"))
    # 只要这组里有一个模型没配单价，费用就算不全，整组标为不可估算
    cost_complete: bool = True
    # 费用是怎么来的：这两个计数器只用来算 cost_basis，不参与任何金额运算
    snapshot_requests: int = 0
    recomputed_requests: int = 0

    @property
    def cost_basis(self) -> str:
        """这组费用是按调用时单价，还是拿当前单价回算的

        没有任何请求时报 snapshot：空组的金额是 0，说它「按当前单价回算」
        会让人以为这里有历史数据需要担心，而实际上什么都没有。
        """
        if self.recomputed_requests <= 0:
            return COST_BASIS_SNAPSHOT
        if self.snapshot_requests <= 0:
            return COST_BASIS_CURRENT
        return COST_BASIS_MIXED

    @property
    def avg_latency_ms(self) -> int:
        """平均耗时，四舍五入到毫秒

        用整数运算而不是 round()：round() 是银行家舍入，150.5 会变成 150、
        151.5 会变成 152，同一份数据看两眼像是在跳动。耗时是给人看的数，
        按常识的「四舍五入」来。
        """
        if self.requests <= 0:
            return 0
        return (self.latency_ms_sum + self.requests // 2) // self.requests

    def add(self, other: "Metrics") -> None:
        self.requests += other.requests
        self.success_count += other.success_count
        self.failed_count += other.failed_count
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.estimated_requests += other.estimated_requests
        self.estimated_tokens += other.estimated_tokens
        self.latency_ms_sum += other.latency_ms_sum
        self.cost += other.cost
        self.cost_complete = self.cost_complete and other.cost_complete
        self.snapshot_requests += other.snapshot_requests
        self.recomputed_requests += other.recomputed_requests

    def to_dict(self, currency: str = "CNY") -> dict:
        """转成响应体。费用算不全时给 None，前端显示「—」而不是 0.00"""
        return {
            "requests": self.requests,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_requests": self.estimated_requests,
            "estimated_tokens": self.estimated_tokens,
            "avg_latency_ms": self.avg_latency_ms,
            "cost": float(quantize_money(self.cost)) if self.cost_complete else None,
            "currency": currency,
            # 金额本身看不出是哪种算法算的，必须显式带出来，
            # 否则「上个月的报表今天打开数变了」永远解释不清
            "cost_basis": self.cost_basis,
            "cost_basis_label": COST_BASIS_LABELS.get(self.cost_basis, ""),
        }


def quantize_money(value: Decimal) -> Decimal:
    """金额统一保留 4 位小数

    不用 2 位：单次调用的费用常在分以下，按 2 位四舍五入会把一整天的零头抹平。
    """
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# 单价的计量单位：每**百万** token。
# 跟着业内报价走（DeepSeek / OpenAI / Anthropic 都按 M token 标价），
# 后台录入时可以照抄厂商价目表，不必先心算除以 1000 ——
# 那种换算迟早会错一个数量级，而错完之后金额看起来仍然「像个正常数字」，
# 极难在报表上被发现。
TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)


# 计价模式（决策 #27，部分修订决策 #21）
BILLING_TOKEN = "token"        # 按 token：输入 / 缓存命中输入 / 输出 三档单价
BILLING_PER_CALL = "per_call"  # 按次：每次调用固定金额，与 token 数无关

ALL_BILLING_MODES = (BILLING_TOKEN, BILLING_PER_CALL)

BILLING_MODE_LABELS = {
    BILLING_TOKEN: "按 token",
    BILLING_PER_CALL: "按次",
}


@dataclass(frozen=True)
class ModelPrice:
    """模型单价快照。缺了必需项就算不出费用（返回 None，绝不返回 0）

    **阶梯 / 分段 / 分时缓存仍然不做**（决策 #21 保留的那部分）。
    真需要时，原始 usage 全在 `usage_records.usage_raw` 里，可以随时重算历史。
    """

    input_per_1m: Optional[Decimal] = None
    output_per_1m: Optional[Decimal] = None
    # 缓存命中的输入 token 通常便宜一大截。
    # **留空 = 「没单独配」，回退到 input_per_1m，不是免费。**
    # 按 0 算等于宣称缓存不要钱，会让费用凭空偏低且没人察觉。
    cached_input_per_1m: Optional[Decimal] = None
    price_per_call: Optional[Decimal] = None
    billing_mode: str = BILLING_TOKEN
    currency: str = "CNY"

    @classmethod
    def from_model(cls, model) -> "ModelPrice":
        """从 models 表的一行（ORM 对象或查询 Row）取单价

        写入侧（记账时快照）和读取侧（报表回算）都走这一个入口。
        两边各抄一遍字段名的话，日后加一档单价必然漏掉一边，
        而漏掉的那边不会报错 —— 只是金额悄悄少一块。
        """
        if model is None:
            return cls()
        return cls(
            input_per_1m=getattr(model, "input_price_per_1m", None),
            output_per_1m=getattr(model, "output_price_per_1m", None),
            # 留空时由 effective_cached_price 回退到输入单价，
            # 不在这里补默认值 ——「没配」和「配成和输入一样」应当可区分
            cached_input_per_1m=getattr(model, "cached_input_price_per_1m", None),
            price_per_call=getattr(model, "price_per_call", None),
            billing_mode=getattr(model, "billing_mode", None) or BILLING_TOKEN,
            currency=getattr(model, "currency", None) or "CNY",
        )

    @property
    def configured(self) -> bool:
        """必需项齐不齐 —— 按模式判定，别拿 token 模式的标准去卡按次模式"""
        if self.billing_mode == BILLING_PER_CALL:
            return self.price_per_call is not None
        return self.input_per_1m is not None and self.output_per_1m is not None

    @property
    def effective_cached_price(self) -> Optional[Decimal]:
        """缓存命中输入的实际单价：没单独配就用输入单价"""
        return (
            self.cached_input_per_1m
            if self.cached_input_per_1m is not None
            else self.input_per_1m
        )


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    price: Optional[ModelPrice],
    cached_prompt_tokens: int = 0,
    requests: int = 1,
) -> Optional[Decimal]:
    """估算费用，单价没配齐就返回 None（不是 0）

    ## 两种模式

    - **按 token**：`(输入-缓存命中)×输入价 + 缓存命中×缓存价 + 输出×输出价`
    - **按次**：`调用次数 × 每次单价`，与 token 数完全无关

    ## 缓存那部分为什么要减出来

    上游返回的 `prompt_tokens` 是**含**缓存命中部分的总数（各家都这样），
    不减掉就会出现「缓存那段按全价又算了一遍」，费用虚高。
    减完之后剩下的才是真正按全价计的新增输入。

    钳到 0 是防脏数据：万一上游把 cached 报得比 prompt 还大（见过），
    直接相减会得到负数，让整份报表的金额莫名其妙变小。

    ⚠️ 这仍是**按当前单价**回算历史用量，上游调价后历史费用会跟着变。
    要让金额不随调价漂移，得在 usage_records 里存单价快照 —— 那是另一件事，
    报表页已标注「估算值，与账单可能有出入」。
    """
    if price is None or not price.configured:
        return None

    if price.billing_mode == BILLING_PER_CALL:
        return Decimal(requests) * price.price_per_call

    cached = max(0, min(cached_prompt_tokens, prompt_tokens))
    fresh = prompt_tokens - cached

    cost = (
        Decimal(fresh) / TOKENS_PER_PRICE_UNIT * price.input_per_1m
        + Decimal(completion_tokens) / TOKENS_PER_PRICE_UNIT * price.output_per_1m
    )

    if cached:
        cost += Decimal(cached) / TOKENS_PER_PRICE_UNIT * price.effective_cached_price

    return cost


def usage_record_metrics(record, prices: Dict[int, ModelPrice]) -> Metrics:
    """把一条当天明细转换成趋势指标，沿用日报的费用完整性语义。"""
    has_snapshot = getattr(record, "billing_mode_snapshot", None) is not None
    stored_cost = getattr(record, "cost", None)

    if has_snapshot:
        cost = Decimal(stored_cost or 0)
        cost_complete = stored_cost is not None
        snapshot_requests = 1
        recomputed_requests = 0
    else:
        price = prices.get(record.model_id) if record.model_id is not None else None
        if price and price.billing_mode == BILLING_PER_CALL and record.status != "success":
            computed = Decimal("0")
        else:
            computed = compute_cost(
                record.prompt_tokens or 0,
                record.completion_tokens or 0,
                price,
                cached_prompt_tokens=record.cached_prompt_tokens or 0,
            )
        cost = computed or Decimal("0")
        cost_complete = computed is not None
        snapshot_requests = 0
        recomputed_requests = 1

    success = 1 if record.status == "success" else 0
    estimated = 1 if record.tokens_estimated else 0
    return Metrics(
        requests=1,
        success_count=success,
        failed_count=1 - success,
        prompt_tokens=record.prompt_tokens or 0,
        completion_tokens=record.completion_tokens or 0,
        total_tokens=record.total_tokens or 0,
        estimated_requests=estimated,
        estimated_tokens=(record.total_tokens or 0) if estimated else 0,
        latency_ms_sum=record.latency_ms or 0,
        cost=cost,
        cost_complete=cost_complete,
        snapshot_requests=snapshot_requests,
        recomputed_requests=recomputed_requests,
    )


@dataclass
class DailyRow:
    """usage_daily 的一行，查询层查出来后转成这个结构再进纯逻辑"""

    stat_date: date
    user_uid: str
    dept_id: Optional[int]
    role: Optional[str]
    model_id: Optional[int]
    model_name: Optional[str]
    requests: int = 0
    success_count: int = 0
    failed_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms_sum: int = 0
    estimated_requests: int = 0
    estimated_tokens: int = 0
    # 缓存命中的输入 token。有它才能按缓存单价计价 ——
    # 只存在明细表的话，算费用就得回头扫明细，那正是分出日聚合表要避免的事
    cached_prompt_tokens: int = 0
    # ---- 单价快照（写入侧按调用时单价当场算好累加进来的）----
    # 三个字段要一起看才有意义，含义见 row_cost()
    cost_sum: Decimal = field(default_factory=lambda: Decimal("0"))
    snapshot_requests: int = 0
    priced_requests: int = 0


def row_cost(
    row: DailyRow, prices: Dict[int, ModelPrice]
) -> Tuple[Optional[Decimal], bool]:
    """一行日聚合的费用，返回 (金额, 是否用了单价快照)

    金额为 None 表示「算不出」——绝不返回 0 冒充（决策 #22）。

    ## 三条分支，顺序不能换

    1. `snapshot_requests < requests`：这一行里混着单价快照上线**之前**的调用，
       `cost_sum` 只覆盖了其中一部分，直接用会漏掉前面那些笔而金额偏小。
       所以整行退回按当前单价回算 —— 与上线前的行为完全一致。
       跨越上线当天的那一行会走到这里，第二天起自然恢复。
       历史行两个计数器都是 0，也天然落在这一支。

    2. `priced_requests < snapshot_requests`：有调用发生时压根没配单价，
       费用**算不全**。这时报 None 而不是「把配了价的那部分加起来」——
       后者会给出一个偏小却看起来很正常的数字，是最危险的那种错。

    3. 其余：全部有快照且全部算得出，直接用 `cost_sum`。
       这也是唯一一条「日后厂商调价、金额纹丝不动」的分支。
    """
    if row.requests > 0 and row.snapshot_requests >= row.requests:
        if row.priced_requests >= row.snapshot_requests:
            return row.cost_sum, True
        return None, True

    price = prices.get(row.model_id) if row.model_id is not None else None
    return compute_cost(
        row.prompt_tokens,
        row.completion_tokens,
        price,
        cached_prompt_tokens=row.cached_prompt_tokens,
        requests=row.requests,
    ), False


def row_metrics(row: DailyRow, prices: Dict[int, ModelPrice]) -> Metrics:
    """把一行日聚合转成指标（含费用估算）"""
    cost, from_snapshot = row_cost(row, prices)

    return Metrics(
        requests=row.requests,
        success_count=row.success_count,
        failed_count=row.failed_count,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        estimated_requests=row.estimated_requests,
        estimated_tokens=row.estimated_tokens,
        latency_ms_sum=row.latency_ms_sum,
        cost=cost if cost is not None else Decimal("0"),
        cost_complete=cost is not None,
        snapshot_requests=row.requests if from_snapshot else 0,
        recomputed_requests=0 if from_snapshot else row.requests,
    )


def group_key(row: DailyRow, dimension: str, granularity: str) -> str:
    """某行在指定维度下的分组键"""
    if dimension == DIMENSION_DATE:
        return bucket_of(row.stat_date, granularity)
    if dimension == DIMENSION_USER:
        return row.user_uid
    if dimension == DIMENSION_DEPT:
        # 部门可能为空（账号还没归属），单独归一类而不是丢掉
        return str(row.dept_id) if row.dept_id is not None else ""
    if dimension == DIMENSION_ROLE:
        return row.role or ""
    if dimension == DIMENSION_MODEL:
        return row.model_name or (str(row.model_id) if row.model_id else "")
    raise UsageQueryError(f"下钻维度非法，可选值：{', '.join(ALL_DIMENSIONS)}")


def aggregate(
    rows: Iterable[DailyRow],
    dimension: str,
    granularity: str,
    prices: Dict[int, ModelPrice],
) -> Dict[str, Metrics]:
    """按维度聚合成 {分组键: 指标}"""
    if dimension not in ALL_DIMENSIONS:
        raise UsageQueryError(f"下钻维度非法，可选值：{', '.join(ALL_DIMENSIONS)}")

    buckets: Dict[str, Metrics] = {}
    for row in rows:
        key = group_key(row, dimension, granularity)
        buckets.setdefault(key, Metrics()).add(row_metrics(row, prices))
    return buckets


def total_metrics(rows: Iterable[DailyRow], prices: Dict[int, ModelPrice]) -> Metrics:
    """整个查询区间的合计"""
    total = Metrics()
    for row in rows:
        total.add(row_metrics(row, prices))
    return total


def cost_basis_note(metrics: Metrics) -> str:
    """费用口径的补充说明，纯快照时为空串

    只在「确实混了回算」时才说话：每份报表都挂一句口径声明，
    真正需要注意的那次反而会被当成背景板略过。
    """
    if metrics.recomputed_requests <= 0:
        return ""
    if metrics.snapshot_requests <= 0:
        return (
            f"其中 {metrics.recomputed_requests} 次调用发生在单价快照上线前，"
            "费用按当前单价回算，会随日后调价变动。"
        )
    return (
        f"其中 {metrics.recomputed_requests} 次调用发生在单价快照上线前，"
        f"费用按当前单价回算；另 {metrics.snapshot_requests} 次按调用时单价计。"
    )


def fill_buckets(start: date, end: date, granularity: str) -> List[str]:
    """区间内的全部时间桶（含没有数据的）

    趋势图缺口不能靠「没有数据就不画点」来表达，否则一条平缓上升的线
    会把中间三天的停机看成正常波动。
    """
    buckets: List[str] = []
    seen = set()
    day = start
    while day <= end:
        key = bucket_of(day, granularity)
        if key not in seen:
            seen.add(key)
            buckets.append(key)
        day += timedelta(days=1)
    return buckets


def sort_groups(
    buckets: Dict[str, Metrics], dimension: str, limit: Optional[int] = None
) -> List[Tuple[str, Metrics]]:
    """分组排序：时间维度按时间正序，其余按 token 量倒序（先看谁用得多）"""
    items = list(buckets.items())
    if dimension == DIMENSION_DATE:
        items.sort(key=lambda kv: kv[0])
    else:
        items.sort(key=lambda kv: (-kv[1].total_tokens, -kv[1].requests, kv[0]))

    if limit is not None and limit > 0:
        items = items[:limit]
    return items


# ==================== 导出 ====================

CSV_HEADERS = [
    "分组",
    "请求数",
    "成功数",
    "失败数",
    "输入token",
    "输出token",
    "合计token",
    "其中估算token",
    "平均耗时(ms)",
    "估算费用",
    "货币",
]


def to_csv(
    rows: List[Tuple[str, Metrics]], currency: str = "CNY", note: str = ""
) -> str:
    """导出 CSV

    Excel 打开 UTF-8 CSV 会乱码，BOM 由路由层加在响应体最前面（这里只出纯文本）。
    费用算不全的行留空而不是写 0，导出的表格同样不能骗人。
    """
    import csv
    import io

    from utils.csv_export import safe_csv_cell

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)

    for label, metrics in rows:
        writer.writerow([
            safe_csv_cell(label),
            metrics.requests,
            metrics.success_count,
            metrics.failed_count,
            metrics.prompt_tokens,
            metrics.completion_tokens,
            metrics.total_tokens,
            metrics.estimated_tokens,
            metrics.avg_latency_ms,
            str(quantize_money(metrics.cost)) if metrics.cost_complete else "",
            currency,
        ])

    if note:
        writer.writerow([])
        writer.writerow([note])

    return buffer.getvalue()
