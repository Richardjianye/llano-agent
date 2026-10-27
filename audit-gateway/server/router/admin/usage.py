"""用量统计查询接口（报表读取侧）

写入侧在 services/usage.py（挂在 /v1/chat/completions 那条路径上），
这里只负责把已经落库的数据切出来。

接口分三层，对应报表页从上到下的三块：

| 接口 | 用途 | 查哪张表 |
| ---- | ---- | -------- |
| `/summary` | 顶部指标卡 + 趋势图 | usage_daily |
| `/breakdown` | 中部按维度下钻的表格 | usage_daily |
| `/records` | 底部单次调用明细 | usage_records（强制分页） |
| `/export` | 导出当前下钻结果 | usage_daily |

**两条不能破的规矩：**

1. 数据范围由 `resolve_usage_scope` 从登录态算出，不看前端传了什么。
   前端传的 dept_id 只是「想看哪个部门」，能不能看是另一回事。
2. 这些接口只返回**指标**，不返回任何聊天内容。会话原文在 conversation_logs，
   走另一套「仅管理部门可见」的权限（决策 #8）。看报表的人不该顺带碰到内容。
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from db.selects.usage import (count_active_chat_users, fetch_daily_rows, fetch_dept_labels,
                              fetch_filter_options, fetch_model_prices,
                              fetch_intraday_records, fetch_records,
                              fetch_user_labels)
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response as RawResponse
from model.response import Response, UserInfo
from services.org_tree import dept_scope_ids
from services.permissions import USAGE_SCOPE_DEPT, resolve_usage_scope
from services.usage_query import (ALL_DIMENSIONS, DIMENSION_DATE,
                                  DIMENSION_DEPT, DIMENSION_MODEL,
                                  DIMENSION_ROLE, DIMENSION_USER,
                                  GRANULARITY_DAY, Metrics, UsageQueryError,
                                  aggregate, bucket_label, cost_basis_note,
                                  fill_buckets, fill_five_minute_buckets,
                                  five_minute_bucket, resolve_range, sort_groups,
                                  to_csv, total_metrics, usage_record_metrics)
from utils.dependencies import require_permission
from utils.errors import ValidationError_

router = APIRouter(prefix="/usage", tags=["用量统计"])

# 报表口径声明。写死在后端而不是前端文案里：导出的 CSV 也要带上这句，
# 否则表格转手几次之后，没人记得这个数字是估算的。
USAGE_NOTE = (
    "数据来自上游返回的 usage 字段，与合作厂商账单可能存在出入；"
    "标记为估算的部分是上游未返回 usage 时按字符数推算的结果；"
    "费用按调用发生时的单价计，早于单价快照上线的历史数据按当前单价回算并单独标注。"
)


async def _scope_of(user: UserInfo):
    """算数据范围，并把部门树展开。

    两步是有意的：`resolve_usage_scope` 保持纯函数（它是这套权限最核心的判定，
    要能脱库直接测），展开这一步需要读库，所以放在这里。
    """
    scope = resolve_usage_scope(
        role=user.permission,
        dept_id=user.dept_id,
        uid=user.uid,
        dept_code=user.dept_code,
    )
    if scope.scope == USAGE_SCOPE_DEPT:
        return scope.widened(await dept_scope_ids(scope.dept_id))
    return scope


async def _label_map(dimension: str, keys: list) -> dict:
    """把分组键翻译成人看得懂的名字"""
    if dimension == DIMENSION_USER:
        return await fetch_user_labels([k for k in keys if k])
    if dimension == DIMENSION_DEPT:
        ids = [int(k) for k in keys if k]
        labels = await fetch_dept_labels(ids)
        return {str(k): v for k, v in labels.items()}
    return {}


def _label_of(dimension: str, key: str, labels: dict, granularity: str) -> str:
    """分组键 → 展示名

    角色维度直接返回角色编码，中文名由前端 constants/roles.ts 渲染 ——
    那份表已经是权限表的镜像，再在后端复制一份中文名只会多一处要同步的地方。
    """
    if dimension == DIMENSION_DATE:
        return bucket_label(key, granularity)
    if not key:
        # 空键代表「未归属」：部门为空、角色为空的历史数据都会落到这里
        return {
            DIMENSION_DEPT: "未分配部门",
            DIMENSION_ROLE: "未知角色",
            DIMENSION_MODEL: "未知模型",
            DIMENSION_USER: "未知用户",
        }.get(dimension, "未知")
    return labels.get(key, key)


@router.get("/summary", response_model=Response)
async def usage_summary(
    start: Optional[date] = None,
    end: Optional[date] = None,
    granularity: str = Query(GRANULARITY_DAY, description="day / week / month"),
    trend_interval: str = Query("auto", pattern="^(auto|5m)$"),
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    user: UserInfo = Depends(require_permission("usage:view")),
):
    """概览：区间合计 + 按时间粒度的趋势

    趋势会把区间内没有数据的桶补成 0 —— 缺口不能靠「不画点」表达，
    否则停机三天会被看成正常波动。
    """
    scope = await _scope_of(user)

    try:
        start, end = resolve_range(start, end, granularity)
    except UsageQueryError as exc:
        raise ValidationError_(str(exc))

    rows = await fetch_daily_rows(
        start, end, scope,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
    )
    prices = await fetch_model_prices()
    active_users = await count_active_chat_users(
        start,
        end,
        scope,
        dept_id=dept_id,
        user_uid=user_uid,
        role=role,
        model_id=model_id,
    )

    total = total_metrics(rows, prices)
    if trend_interval == "5m":
        if start != end:
            raise ValidationError_("5 分钟趋势只支持查询同一天")
        intraday_rows = await fetch_intraday_records(
            start,
            scope,
            dept_id=dept_id,
            user_uid=user_uid,
            role=role,
            model_id=model_id,
        )
        buckets: dict[str, Metrics] = {}
        for row in intraday_rows:
            key = five_minute_bucket(row.created_at)
            buckets.setdefault(key, Metrics()).add(usage_record_metrics(row, prices))
        trend = [
            {
                "bucket": key,
                "label": key[11:16],
                "metrics": buckets.get(key, Metrics()).to_dict(),
            }
            for key in fill_five_minute_buckets(start)
        ]
    else:
        buckets = aggregate(rows, DIMENSION_DATE, granularity, prices)
        trend = [
            {
                "bucket": key,
                "label": bucket_label(key, granularity),
                "metrics": buckets.get(key, Metrics()).to_dict(),
            }
            for key in fill_buckets(start, end, granularity)
        ]

    return Response(data={
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "granularity": granularity,
        "scope": scope.scope,
        "active_users": active_users,
        "trend_interval": trend_interval,
        "total": total.to_dict(),
        "trend": trend,
        "note": USAGE_NOTE,
        # 只有确实混了「按当前单价回算」的数据时才非空，
        # 前端据此在费用旁挂一句提示，而不是每次都挂
        "cost_note": cost_basis_note(total),
    })


@router.get("/breakdown", response_model=Response)
async def usage_breakdown(
    dimension: str = Query(DIMENSION_USER, description="date / user / dept / role / model"),
    start: Optional[date] = None,
    end: Optional[date] = None,
    granularity: str = Query(GRANULARITY_DAY),
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=1000),
    user: UserInfo = Depends(require_permission("usage:view")),
):
    """按任意维度下钻。筛选条件可叠加，实现「部门 A 里谁用得最多」这类问题"""
    if dimension not in ALL_DIMENSIONS:
        raise ValidationError_(f"下钻维度非法，可选值：{', '.join(ALL_DIMENSIONS)}")

    scope = await _scope_of(user)

    try:
        start, end = resolve_range(start, end, granularity)
    except UsageQueryError as exc:
        raise ValidationError_(str(exc))

    rows = await fetch_daily_rows(
        start, end, scope,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
    )
    prices = await fetch_model_prices()

    buckets = aggregate(rows, dimension, granularity, prices)
    ordered = sort_groups(buckets, dimension, limit=limit)
    labels = await _label_map(dimension, [k for k, _ in ordered])
    total = total_metrics(rows, prices)

    return Response(data={
        "dimension": dimension,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "scope": scope.scope,
        "items": [
            {
                "key": key,
                "label": _label_of(dimension, key, labels, granularity),
                "metrics": metrics.to_dict(),
            }
            for key, metrics in ordered
        ],
        "total": total.to_dict(),
        "note": USAGE_NOTE,
        "cost_note": cost_basis_note(total),
    })


@router.get("/records", response_model=Response)
async def usage_records(
    start: Optional[date] = None,
    end: Optional[date] = None,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: UserInfo = Depends(require_permission("usage:view")),
):
    """单次调用明细 —— 排查「这笔怎么这么贵」「上游那天抖了几次」

    只有这个接口会查 usage_records，且强制分页。
    返回的是指标不是内容：要看聊天原文请走会话审计（仅管理部门可见）。
    """
    scope = await _scope_of(user)

    try:
        start, end = resolve_range(start, end, GRANULARITY_DAY)
    except UsageQueryError as exc:
        raise ValidationError_(str(exc))

    rows, total = await fetch_records(
        start, end, scope,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
        status=status,
        limit=page_size, offset=(page - 1) * page_size,
    )

    user_labels = await fetch_user_labels(list({r.user_uid for r in rows}))
    dept_labels = await fetch_dept_labels(
        list({r.dept_id for r in rows if r.dept_id is not None})
    )

    return Response(data={
        "items": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "user_uid": r.user_uid,
                "username": user_labels.get(r.user_uid, r.user_uid),
                "dept_id": r.dept_id,
                "dept_name": dept_labels.get(r.dept_id) if r.dept_id else None,
                "role": r.role,
                "model_name": r.model_name,
                "stream": bool(r.stream),
                "status": r.status,
                "error_code": r.error_code,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "cached_prompt_tokens": r.cached_prompt_tokens or 0,
                "cache_write_tokens": r.cache_write_tokens or 0,
                "reasoning_tokens": r.reasoning_tokens or 0,
                "tokens_estimated": bool(r.tokens_estimated),
                # 与 status 正交：中断的调用**是成功的调用**，token 照样花了
                "aborted": bool(r.aborted),
                "latency_ms": r.latency_ms,
                # 单价快照：None 表示这笔当时没配单价（或早于快照上线），
                # 前端显示「—」而不是 0.00 —— 这是明细页存在的意义之一，
                # 「这笔怎么这么贵」要能一路追到当时按什么价算的
                "cost": float(r.cost) if r.cost is not None else None,
                "cost_currency": r.cost_currency,
                "billing_mode": r.billing_mode_snapshot,
                "input_price_per_1m": (
                    float(r.input_price_per_1m_snapshot)
                    if r.input_price_per_1m_snapshot is not None else None
                ),
                "output_price_per_1m": (
                    float(r.output_price_per_1m_snapshot)
                    if r.output_price_per_1m_snapshot is not None else None
                ),
                "cached_input_price_per_1m": (
                    float(r.cached_input_price_per_1m_snapshot)
                    if r.cached_input_price_per_1m_snapshot is not None else None
                ),
                "price_per_call": (
                    float(r.price_per_call_snapshot)
                    if r.price_per_call_snapshot is not None else None
                ),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "note": USAGE_NOTE,
    })


@router.get("/filters", response_model=Response)
async def usage_filters(user: UserInfo = Depends(require_permission("usage:view"))):
    """筛选下拉框的候选项（只列范围内确实有用量的部门与模型）"""
    scope = await _scope_of(user)
    options = await fetch_filter_options(scope)
    return Response(data={**options, "scope": scope.scope})


@router.get("/export")
async def usage_export(
    dimension: str = Query(DIMENSION_USER),
    start: Optional[date] = None,
    end: Optional[date] = None,
    granularity: str = Query(GRANULARITY_DAY),
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    user: UserInfo = Depends(require_permission("usage:view")),
):
    """导出当前下钻结果为 CSV

    不套 {code,message,data} 信封 —— 这里返回的是文件流，
    浏览器直接下载，套信封就变成一个内容是 JSON 的 .csv 了。
    """
    if dimension not in ALL_DIMENSIONS:
        raise ValidationError_(f"下钻维度非法，可选值：{', '.join(ALL_DIMENSIONS)}")

    scope = await _scope_of(user)

    try:
        start, end = resolve_range(start, end, granularity)
    except UsageQueryError as exc:
        raise ValidationError_(str(exc))

    rows = await fetch_daily_rows(
        start, end, scope,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
    )
    prices = await fetch_model_prices()

    buckets = aggregate(rows, dimension, granularity, prices)
    ordered = sort_groups(buckets, dimension)
    labels = await _label_map(dimension, [k for k, _ in ordered])

    labelled = [
        (_label_of(dimension, key, labels, granularity), metrics)
        for key, metrics in ordered
    ]
    # 口径必须跟着文件走：CSV 转手几次之后，没人记得这份金额是怎么算出来的
    cost_note = cost_basis_note(total_metrics(rows, prices))
    csv_text = to_csv(
        labelled,
        note=(
            f"{USAGE_NOTE}（统计区间 {start} ~ {end}，服务器本地时区）"
            + (f" {cost_note}" if cost_note else "")
        ),
    )

    filename = f"usage-{dimension}-{start}-{end}.csv"
    return RawResponse(
        # Excel 打开无 BOM 的 UTF-8 CSV 会把中文显示成乱码，这里必须加
        content=("﻿" + csv_text).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
