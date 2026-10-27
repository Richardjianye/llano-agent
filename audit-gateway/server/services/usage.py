"""用量统计与会话审计的写入

三张表分开写：
- conversation_logs：聊天原文，仅管理部门可见
- conversation_attachments：图片/文件的元数据与落盘位置（内联 base64 会撑爆正文列，
  所以抽出来单独存，正文里留占位符；拆解逻辑在 services/attachments.py）
- usage_records + usage_daily：只有指标，部门负责人看报表时碰不到内容

日聚合用 upsert 实时累加，而不是半夜跑批：报表要能看到「今天用了多少」，
而且省掉一个需要监控的定时任务。代价是每次调用多一次写，量级完全扛得住。

**写入侧的原则：能存的都先存下来。** token 细分（缓存命中/写入、推理 token）
和上游 usage 原文都会落库，哪怕现在的报表根本没用到——
成本计算规则以后再定都来得及，但这一刻没记下来的 token 明细，事后谁也补不回来。

**单价也是「这一刻不记就补不回来」的东西。** 记账时把当时生效的单价与算出的
费用一并落库（见 build_price_snapshot），报表才不会拿今天的价去乘上个月的
token —— 那会让一份已经发出去的报表在厂商调价后自己变数，与厂商账单也永远对不上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from loguru import logger
from services.usage_query import BILLING_PER_CALL, ModelPrice, compute_cost

from services.audit_content import (make_user_preview, shape_audit_content,
                                    truncate_text)
from services.audit_query import PREVIEW_MAX_CHARS


@dataclass
class UsageFacts:
    """一次转发调用的全部可记录信息"""

    request_id: str
    user_uid: str
    api_key_id: Optional[int] = None
    dept_id: Optional[int] = None
    role: Optional[str] = None
    provider_id: Optional[int] = None
    model_id: Optional[int] = None
    model_name: Optional[str] = None
    stream: bool = False
    status: str = "success"
    error_code: Optional[str] = None
    # 与 status 正交：客户端点了「停止生成」不是失败，token 照样消耗
    aborted: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # token 细分与原文：成本计算以后再做，但数据现在就要存全，事后补不回来
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    usage_raw: Optional[dict] = None
    tokens_estimated: bool = False
    latency_ms: int = 0
    # 调用时生效的单价。由转发链路从 models 表那一行直接取（ModelPrice.from_model），
    # 而不是记账时回查数据库：回查拿到的是「记账那一刻的价」，
    # 若正好有人在后台改价，账就记到新价上去了
    price: Optional[ModelPrice] = None
    # 审计内容
    request_messages: List[dict] = field(default_factory=list)
    response_text: str = ""
    masked: bool = False
    # 会话串联（决策 #52）：客户端在私有信封里给的，已在 audit_envelope 里
    # 校验降级过。**不可信、不参与鉴权**，只决定审计页怎么分组和折叠
    session_id: Optional[str] = None
    turn_kind: str = "chat"
    # ---- 群聊维度（阶段 27-E）----
    #
    # 两者都由服务端从**凭据**推出来（群聊会话令牌带着 scope=group +
    # scope_project_id），不听请求头 —— 客户端能声明的话，「这笔算个人还是算
    # 协作」就成了自选项，而报表要靠它拆开这两种消耗。
    #
    # call_kind=gate 是 P1 的语义路由打分：照常计费，但默认不计入对话请求数
    # 与上游成功率，否则「上游稳不稳」这个指标会被一堆没人看见的打分稀释。
    call_kind: str = "chat"
    trigger_source: str = "private"


def estimate_tokens(text: str) -> int:
    """上游没返回 usage 时的兜底估算

    中英混排按 ~2 字符/token 粗算。这个数只用于「有总比没有强」的趋势观察，
    写库时会打 tokens_estimated 标记，报表必须把估算和实测分开展示。
    """
    if not text:
        return 0
    return max(1, len(text) // 2)


def extract_usage(payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """从上游响应里取 usage"""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")

    if prompt is None and completion is None and total is None:
        return None

    prompt = int(prompt or 0)
    completion = int(completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(total or (prompt + completion)),
    }


def _int(value: Any) -> int:
    """上游偶尔会把数字发成字符串或 null，这里统一收敛，别让统计写入炸掉"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _dig(payload: Any, *path: str) -> Any:
    """安全地取嵌套字段，任一层不是 dict 就返回 None"""
    cur = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


@dataclass
class UsageBreakdown:
    """token 细分 —— 计费口径的原料

    各家字段名不统一，这里做一次归一化，同时把 raw 原样带上：

    | 含义 | OpenAI 兼容 | DeepSeek | Anthropic |
    | ---- | ----------- | -------- | --------- |
    | 缓存命中 | `prompt_tokens_details.cached_tokens` | `prompt_cache_hit_tokens` | `cache_read_input_tokens` |
    | 缓存写入 | —— | —— | `cache_creation_input_tokens` |
    | 推理 token | `completion_tokens_details.reasoning_tokens` | 同左 | —— |

    **归一化只是为了报表好查，不是为了算钱。** 认不出来的字段（分时缓存的
    5m/1h 分档、按次计费项、厂商私有扩展）全在 raw 里，将来定价规则定下来了
    直接拿 raw 重算即可，不用回头改写入侧、更不用重跑历史。
    """

    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    raw: Optional[dict] = None


def extract_breakdown(payload: Dict[str, Any]) -> UsageBreakdown:
    """从上游响应里取 token 细分。取不到就是 0，不影响主流程"""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return UsageBreakdown()

    cached = (
        _dig(usage, "prompt_tokens_details", "cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
    )
    cache_write = (
        usage.get("cache_creation_input_tokens")
        or _dig(usage, "prompt_tokens_details", "cache_creation_tokens")
    )
    reasoning = (
        _dig(usage, "completion_tokens_details", "reasoning_tokens")
        or usage.get("reasoning_tokens")
    )

    return UsageBreakdown(
        cached_prompt_tokens=_int(cached),
        cache_write_tokens=_int(cache_write),
        reasoning_tokens=_int(reasoning),
        raw=usage,
    )


# ==================== 单价快照 ====================

# 明细表 cost 列是 DECIMAL(18,6)，这里先量化到同一精度，
# 免得「库里存的」和「刚算出来的」差在第 7 位小数上，对账时白白多一层疑问。
# 代价是每笔最多 5e-7 元的舍入，几十万笔也凑不满一分钱。
_COST_QUANTUM = Decimal("0.000001")


@dataclass
class PriceSnapshot:
    """一次调用的定价结果

    cost 为 None 表示**当时没配单价，算不出**，与 0（确实免费）是两回事。
    报表据此把整组标成「算不全」，而不是按 0 混进总额（决策 #22）。
    """

    cost: Optional[Decimal] = None
    columns: Dict[str, Any] = field(default_factory=dict)

    @property
    def priced(self) -> bool:
        return self.cost is not None


def build_price_snapshot(facts: "UsageFacts") -> PriceSnapshot:
    """按调用当时的单价算出这一笔的费用，并整理出要落库的单价列

    纯函数，不碰数据库 —— 计价是「错了也不报错、只是金额悄悄不对」的地方，
    必须能脱离 MySQL 单测。

    ## 失败的调用不按次计费

    按 token 计价时失败调用的 token 本来就是 0，费用自然是 0。
    但**按次计价**不看 token：不特判的话，上游 502 一次也要收一次的钱，
    月底报表里就会凭空多出一批「花了钱但什么都没拿到」的调用。
    中断（aborted）不在此列 —— 那是成功的调用，内容已经产出，只是没人看完。
    """
    price = facts.price
    columns: Dict[str, Any] = {
        "billing_mode_snapshot": price.billing_mode if price else None,
        "input_price_per_1m_snapshot": price.input_per_1m if price else None,
        "output_price_per_1m_snapshot": price.output_per_1m if price else None,
        # 留空原样留空：它的含义是「当时没单独配缓存价，按输入价计」，
        # 在这里替换成输入价会把「没配」和「配成一样」两种情况抹平
        "cached_input_price_per_1m_snapshot": (
            price.cached_input_per_1m if price else None
        ),
        "price_per_call_snapshot": price.price_per_call if price else None,
        "cost_currency": price.currency if price else None,
    }

    if price is None or not price.configured:
        # 单价没配齐：单价列该记的照记（便于事后解释「为什么这笔算不出」），
        # 费用留 NULL
        columns["cost"] = None
        return PriceSnapshot(cost=None, columns=columns)

    if price.billing_mode == BILLING_PER_CALL and facts.status != "success":
        cost = Decimal("0")
    else:
        cost = compute_cost(
            facts.prompt_tokens,
            facts.completion_tokens,
            price,
            cached_prompt_tokens=facts.cached_prompt_tokens,
            requests=1,
        )

    if cost is not None:
        cost = cost.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)

    columns["cost"] = cost
    return PriceSnapshot(cost=cost, columns=columns)


async def record_usage(facts: UsageFacts, stat_day: Optional[date] = None) -> None:
    """写用量明细 + 累加日聚合 + 写会话内容

    整个函数吞掉异常：统计写失败不该让用户的对话请求跟着失败，
    但一定要留 error 日志，否则报表少了数据没人知道。
    """
    try:
        await _write_usage(facts, stat_day or date.today())
    except Exception as exc:
        logger.error(f"写用量记录失败 rid={facts.request_id}: {exc}")

    try:
        await _write_conversation(facts)
    except Exception as exc:
        logger.error(f"写会话审计失败 rid={facts.request_id}: {exc}")


async def _write_usage(facts: UsageFacts, stat_day: date) -> None:
    from sqlalchemy import insert
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    from db import init_async_session
    from model.database import UsageDaily, UsageRecord

    # 定价在写库之前算好：算的是「调用发生时」的价，不是「写库时」的价
    snapshot = build_price_snapshot(facts)

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            insert(UsageRecord).values(
                **snapshot.columns,
                request_id=facts.request_id,
                api_key_id=facts.api_key_id,
                stat_date=stat_day,
                user_uid=facts.user_uid,
                dept_id=facts.dept_id,
                role=facts.role,
                provider_id=facts.provider_id,
                model_id=facts.model_id,
                model_name=facts.model_name,
                stream=facts.stream,
                status=facts.status,
                error_code=facts.error_code,
                call_kind=facts.call_kind,
                trigger_source=facts.trigger_source,
                aborted=facts.aborted,
                prompt_tokens=facts.prompt_tokens,
                completion_tokens=facts.completion_tokens,
                total_tokens=facts.total_tokens,
                cached_prompt_tokens=facts.cached_prompt_tokens,
                cache_write_tokens=facts.cache_write_tokens,
                reasoning_tokens=facts.reasoning_tokens,
                usage_raw=(
                    json.dumps(facts.usage_raw, ensure_ascii=False)
                    if facts.usage_raw else None
                ),
                tokens_estimated=facts.tokens_estimated,
                latency_ms=facts.latency_ms,
            )
        )

        success = 1 if facts.status == "success" else 0
        # 估算与实测分开累计：报表要能标出「这份数字里有多少是猜的」
        est_requests = 1 if facts.tokens_estimated else 0
        est_tokens = facts.total_tokens if facts.tokens_estimated else 0
        # 快照计数器：每笔都 +1（哪怕算不出费用），算得出的才进 priced。
        # 报表靠 snapshot_requests 是否等于 requests 判断这一行的费用可不可信，
        # 所以这个 +1 绝不能因为「这笔没定价」而跳过
        priced = 1 if snapshot.priced else 0
        cost_value = snapshot.cost or Decimal("0")
        currency = snapshot.columns.get("cost_currency")

        stmt = mysql_insert(UsageDaily).values(
            stat_date=stat_day,
            user_uid=facts.user_uid,
            dept_id=facts.dept_id,
            role=facts.role,
            model_id=facts.model_id,
            model_name=facts.model_name,
            requests=1,
            success_count=success,
            failed_count=1 - success,
            prompt_tokens=facts.prompt_tokens,
            completion_tokens=facts.completion_tokens,
            total_tokens=facts.total_tokens,
            cached_prompt_tokens=facts.cached_prompt_tokens,
            latency_ms_sum=facts.latency_ms,
            estimated_requests=est_requests,
            estimated_tokens=est_tokens,
            cost_sum=cost_value,
            snapshot_requests=1,
            priced_requests=priced,
            cost_currency=currency,
        )
        # ON DUPLICATE KEY UPDATE：命中 (日期, 用户, 模型) 唯一键就累加
        await session.execute(
            stmt.on_duplicate_key_update(
                requests=UsageDaily.requests + 1,
                success_count=UsageDaily.success_count + success,
                failed_count=UsageDaily.failed_count + (1 - success),
                prompt_tokens=UsageDaily.prompt_tokens + facts.prompt_tokens,
                completion_tokens=UsageDaily.completion_tokens + facts.completion_tokens,
                total_tokens=UsageDaily.total_tokens + facts.total_tokens,
                cached_prompt_tokens=(
                    UsageDaily.cached_prompt_tokens + facts.cached_prompt_tokens
                ),
                latency_ms_sum=UsageDaily.latency_ms_sum + facts.latency_ms,
                estimated_requests=UsageDaily.estimated_requests + est_requests,
                estimated_tokens=UsageDaily.estimated_tokens + est_tokens,
                cost_sum=UsageDaily.cost_sum + cost_value,
                snapshot_requests=UsageDaily.snapshot_requests + 1,
                priced_requests=UsageDaily.priced_requests + priced,
                dept_id=facts.dept_id,
                role=facts.role,
                # 只在这次确实有货币时才覆盖：没定价的调用带的是 NULL，
                # 写进去会把这一行早先记下的货币抹掉
                **({"cost_currency": currency} if currency else {}),
            )
        )
        await session.commit()


async def _write_conversation(facts: UsageFacts) -> None:
    """写会话原文 + 附件记录

    **附件必须在序列化之前抽走。** 一张 base64 内联的截图有几 MB，
    直接 json.dumps 之后 `_truncate` 会从中间一刀切下去，
    审计员打开详情看到的是半串 base64，最想看的文字反而被截没了。
    所以先拆信封：附件本体落盘、元数据入表，正文里只留 `[[attachment:...]]`。

    抽取用的是**审计副本**，发给上游的原件早在 chat.py 里发出去了，
    这里怎么改都不会影响用户看到的模型回复。
    """
    import asyncio

    from sqlalchemy import insert

    from config import get_settings
    from db import init_async_session
    from model.database import ConversationAttachment, ConversationLog
    from services.attachments import extract_attachments, store_attachments

    settings = get_settings()

    audit_messages, attachments = extract_attachments(facts.request_messages)

    if attachments:
        # 落盘是阻塞 IO，扔进线程池别占着事件循环。
        # store_attachments 内部吞掉所有异常，这里不需要再 try
        await asyncio.to_thread(
            store_attachments,
            attachments,
            root=settings.audit_storage_path,
            max_bytes=settings.audit_attachment_max_bytes,
            enabled=settings.audit_store_attachment_bytes,
        )

    # 内容模型见 services/audit_content.py：用户输入全文、系统提示只留指纹、
    # 工具链只留骨架、思考链不存（决策 #52）
    shaped = shape_audit_content(audit_messages)

    # 摘要取自**整形之前**的原文：正文可能因为超长被裁，但列表页那一句
    # 「用户到底问了什么」不该跟着一起丢。
    # 保头也保尾：粘一段长文档再提问时，问题在最后一行，只截头就等于每行都显示
    # 同一段文档开头（见 services/audit_content.py 模块注释）
    preview = make_user_preview(shaped.user_input, PREVIEW_MAX_CHARS)

    response_text, resp_truncated = truncate_text(facts.response_text)

    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(ConversationLog).values(
                request_id=facts.request_id,
                user_uid=facts.user_uid,
                session_id=facts.session_id,
                turn_kind=facts.turn_kind,
                dept_id=facts.dept_id,
                role=facts.role,
                model_id=facts.model_id,
                model_name=facts.model_name,
                user_input=shaped.user_input,
                last_user_preview=preview,
                system_prompt_sha256=shaped.system_prompt_sha256,
                system_prompt_chars=shaped.system_prompt_chars,
                tool_skeleton=shaped.tool_skeleton,
                response_text=response_text,
                masked=facts.masked,
                truncated=shaped.truncated or resp_truncated,
                status=facts.status,
                stream=facts.stream,
                aborted=facts.aborted,
                attachment_count=len(attachments),
            )
        )

        conversation_id = result.inserted_primary_key[0] if result.inserted_primary_key else None

        if attachments:
            await session.execute(
                insert(ConversationAttachment),
                [
                    {
                        "request_id": facts.request_id,
                        "conversation_id": conversation_id,
                        "user_uid": facts.user_uid,
                        "dept_id": facts.dept_id,
                        "marker": att.marker,
                        "kind": att.kind,
                        "mime_type": att.mime_type,
                        "filename": (att.filename or "")[:255] or None,
                        "size_bytes": att.size_bytes,
                        "sha256": att.sha256,
                        "origin_url": att.origin_url,
                        "storage_path": att.storage_path,
                        "stored": att.stored,
                        "store_error": att.store_error,
                    }
                    for att in attachments
                ],
            )

        await session.commit()
