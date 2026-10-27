"""会话审计查询 —— 读取侧的数据库访问

## 与用量查询的关键差异：这里没有 scope

用量报表要算 `resolve_usage_scope`（总裁/开发者全公司，审计/部门负责人限本部门）。
会话审计**不需要**，因为可见性判定完全落在 `can_view_audit` 上：
进得来就能看全公司，进不来一条都看不到（决策 #8、#17）。

两套规则相互独立，别把 usage 那套 scope 复制过来 —— 管理部门审计看得到
全公司聊天内容，但用量报表里他只能看本部门。这不是矛盾，是刻意设计。

## 每个函数都强制带时间范围

`conversation_logs` 随调用量线性增长（TODO 风险表里的中级风险）。
一个不带条件的 `SELECT *` 就能把库拖垮，所以时间范围不是可选参数。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Dict, List, Optional, Tuple

from db import init_async_session
from model.database import (AuditAccessLog, ConversationAttachment,
                            ConversationLog, Department, ModelInfo, UsageRecord,
                            User)
from sqlalchemy import Select, case, func, or_, select


def _day_bounds(start: date, end: date) -> Tuple[datetime, datetime]:
    """把「日期区间」翻译成「时间戳区间」

    `created_at` 是 DATETIME，直接拿 date 比较会漏掉 end 当天 00:00 之后的数据。
    右端取 end 的 23:59:59.999999 而不是 end+1 天的 00:00：
    闭区间的语义和用量报表保持一致，省得两个页面对同一天算出不同的数。
    """
    return datetime.combine(start, time.min), datetime.combine(end, time.max)


def _apply_filters(
    stmt: Select,
    start: date,
    end: date,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    status: Optional[str] = None,
    has_attachment: Optional[bool] = None,
    keyword: Optional[str] = None,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    include_machine_turns: bool = False,
) -> Select:
    """把筛选条件翻译成 WHERE。keyword 必须是已转义过的（见 audit_query.normalize_keyword）"""
    lower, upper = _day_bounds(start, end)
    stmt = stmt.where(
        ConversationLog.created_at >= lower, ConversationLog.created_at <= upper
    )

    if dept_id is not None:
        stmt = stmt.where(ConversationLog.dept_id == dept_id)
    if user_uid:
        stmt = stmt.where(ConversationLog.user_uid == user_uid)
    if role:
        stmt = stmt.where(ConversationLog.role == role)
    if model_id is not None:
        stmt = stmt.where(ConversationLog.model_id == model_id)
    if request_id:
        # 从用量明细追到会话原文的入口：明细页复制请求ID，粘进来直接定位
        stmt = stmt.where(ConversationLog.request_id == request_id)
    if session_id:
        # 展开某个会话时用。⚠️ session_id 是客户端给的、可伪造 ——
        # 它只是个筛选条件，**不是权限边界**：进得来审计页的人本来就看得到
        # 全公司内容（决策 #8），这里不存在「填别人的 ID 越权」这回事。
        # 真正要守的是分组时别把两个人的记录混进同一行，那由 GROUP BY 里的
        # user_uid 保证（见 fetch_sessions）
        stmt = stmt.where(ConversationLog.session_id == session_id)
    if not include_machine_turns:
        # 标题生成、命令审批扫描这些机器轮次默认不显示 —— 它们把「第一个请求
        # 总是失败」这类噪声顶到列表最前面。**只是不显示，全量存储不变**
        stmt = stmt.where(ConversationLog.turn_kind == "chat")
    if status:
        stmt = stmt.where(ConversationLog.status == status)
    if has_attachment is True:
        stmt = stmt.where(ConversationLog.attachment_count > 0)
    elif has_attachment is False:
        stmt = stmt.where(ConversationLog.attachment_count == 0)

    if keyword:
        # 请求和回复都搜。这是 LIKE '%x%' 全表扫描，明知慢仍然这么做：
        # 会话内容检索没法靠 B-Tree 索引，真到扛不住那天再上全文索引或 ES。
        # 时间范围是强制的，实际扫描量被区间卡住了。
        pattern = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                ConversationLog.user_input.like(pattern, escape="\\"),
                ConversationLog.response_text.like(pattern, escape="\\"),
            )
        )

    return stmt


async def fetch_conversations(
    start: date,
    end: date,
    limit: int,
    offset: int,
    with_tools: bool = False,
    **filters,
) -> Tuple[List[ConversationLog], int]:
    """分页查会话列表 + 总数

    **故意不 select 全部列**：`user_input` / `response_text` 是两个 TEXT，
    一页 20 条能拉回几百 KB 甚至几 MB。列表只需要摘要，摘要由路由层从
    截断后的片段生成 —— 这里用 SQL 的 LEFT() 只取前一小段回来。
    """
    from services.audit_query import PREVIEW_MAX_CHARS

    # `last_user_preview` 是入库时就落定的一列，直接取即可。
    # `request_head` 只为**老数据**保留：决策 #52 之前写进去的行没有摘要可读，
    # 只能退回「LEFT() 取开头再拍平」。那条路对 Hermes 的老记录效果很差
    # （开头永远是系统提示），但比空白强，且不会随时间恶化 —— 新记录都有摘要
    slice_len = PREVIEW_MAX_CHARS * 8

    stmt = select(
        ConversationLog.id,
        ConversationLog.request_id,
        ConversationLog.created_at,
        ConversationLog.user_uid,
        ConversationLog.dept_id,
        ConversationLog.role,
        ConversationLog.model_id,
        ConversationLog.model_name,
        ConversationLog.status,
        ConversationLog.masked,
        ConversationLog.truncated,
        ConversationLog.stream,
        ConversationLog.aborted,
        ConversationLog.attachment_count,
        ConversationLog.session_id,
        ConversationLog.turn_kind,
        ConversationLog.last_user_preview,
        func.left(ConversationLog.user_input, slice_len).label("request_head"),
        func.left(ConversationLog.response_text, slice_len).label("response_head"),
    )

    if with_tools:
        # ⚠️ 整列取回，**绝不 LEFT()**：那一列是 JSON，切字符串会切出半截数组
        # （这个项目为这件事付过一次代价，见 services/audit_content.py 模块注释）。
        # 体积自身有界：条数上限 TOOL_SKELETON_MAX_ENTRIES，每条只有名字和字数。
        #
        # 默认关是因为**导出**也走这个函数，而导出一次拉 EXPORT_MAX_ROWS 行 ——
        # 那种量级上每行多带几 KB 就很可观了，而导出的 CSV 里根本没有这一列
        stmt = stmt.add_columns(ConversationLog.tool_skeleton)
    stmt = _apply_filters(stmt, start, end, **filters)
    stmt = stmt.order_by(ConversationLog.id.desc()).limit(limit).offset(offset)

    count_stmt = _apply_filters(
        select(func.count(ConversationLog.id)), start, end, **filters
    )

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(stmt)).all()
        total = (await session.execute(count_stmt)).scalar() or 0

    return rows, total


async def fetch_conversation(conversation_id: int) -> Optional[ConversationLog]:
    """按 ID 取单条会话全文 —— 详情抽屉唯一的数据来源

    只有这个函数会把两个 TEXT 列整体拉回来，所以它必然对应一条
    `detail` 查阅日志。调用方漏记日志就是决策 #8 没落实。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(ConversationLog).where(ConversationLog.id == conversation_id)
            )
        ).scalar_one_or_none()


async def fetch_attachments(conversation_id: int) -> List[ConversationAttachment]:
    """某条会话的附件列表"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        return list(
            (
                await session.execute(
                    select(ConversationAttachment)
                    .where(ConversationAttachment.conversation_id == conversation_id)
                    .order_by(ConversationAttachment.id.asc())
                )
            ).scalars().all()
        )


async def fetch_attachment(attachment_id: int) -> Optional[ConversationAttachment]:
    """按 ID 取单个附件 —— 下载接口用"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(ConversationAttachment).where(
                    ConversationAttachment.id == attachment_id
                )
            )
        ).scalar_one_or_none()


async def fetch_audit_filter_options() -> Dict[str, list]:
    """筛选下拉框候选项

    与用量那边不同，这里列的是**全部**部门、模型和在职用户，不是「有记录的」：
    审计员经常要确认「某个人/某个部门有没有用过 AI」，
    下拉框里干脆没有这个人的话，这个问题就问不出来了 ——
    而「查了但没有记录」和「压根查不了」是两个完全不同的结论。

    用户列表带上部门，前端下拉里显示成「admin · 管理部门」：
    公司里同名同姓不罕见，只给花名会选错人。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        depts = (
            await session.execute(
                select(Department.id, Department.name).order_by(Department.id)
            )
        ).all()
        models = (
            await session.execute(
                select(ModelInfo.id, ModelInfo.model_name).order_by(ModelInfo.id)
            )
        ).all()
        users = (
            await session.execute(
                select(
                    User.uid, User.username, User.realname,
                    User.dept_id, User.permission,
                ).order_by(User.dept_id, User.id)
            )
        ).all()

    dept_names = {d[0]: d[1] for d in depts}

    return {
        "departments": [{"id": d[0], "name": d[1]} for d in depts],
        "models": [{"id": m[0], "name": m[1]} for m in models],
        "users": [
            {
                "uid": u[0],
                "name": u[2] or u[1] or u[0],
                "dept_id": u[3],
                "dept_name": dept_names.get(u[3]),
                "role": u[4],
            }
            for u in users
        ],
    }


async def fetch_user_labels(uids: List[str]) -> Dict[str, str]:
    """uid → 花名（决策 #19：用户名直接是中文花名）"""
    if not uids:
        return {}

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(User.uid, User.username, User.realname).where(User.uid.in_(uids))
            )
        ).all()

    return {r[0]: (r[2] or r[1] or r[0]) for r in rows}


async def fetch_dept_labels(dept_ids: List[int]) -> Dict[int, str]:
    if not dept_ids:
        return {}

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(Department.id, Department.name).where(Department.id.in_(dept_ids))
            )
        ).all()

    return {r[0]: r[1] for r in rows}


async def fetch_access_logs(
    start: date,
    end: date,
    limit: int,
    offset: int,
    actor_uid: Optional[str] = None,
    action: Optional[str] = None,
    target_user_uid: Optional[str] = None,
) -> Tuple[List[AuditAccessLog], int]:
    """查阅日志的分页查询 —— 「谁看了谁的什么」

    这张表本身也要能被看到，否则「记了日志」和「没记」在体感上没区别。
    看查阅日志同样需要 audit:view，但**不再为此记一条日志**：
    否则打开这个页面就会产生新记录，翻两页就把真正的查阅行为淹没了。

    `target_user_uid`（按被查阅人筛选）是这张表最有价值的用法：
    按查阅人翻只能看出「这个审计员很勤快」，按被查阅人翻才回答得了
    「有没有人在专门盯着某个人的会话」。
    """
    lower, upper = _day_bounds(start, end)

    stmt = select(AuditAccessLog).where(
        AuditAccessLog.created_at >= lower, AuditAccessLog.created_at <= upper
    )
    count_stmt = select(func.count(AuditAccessLog.id)).where(
        AuditAccessLog.created_at >= lower, AuditAccessLog.created_at <= upper
    )

    if actor_uid:
        stmt = stmt.where(AuditAccessLog.actor_uid == actor_uid)
        count_stmt = count_stmt.where(AuditAccessLog.actor_uid == actor_uid)
    if action:
        stmt = stmt.where(AuditAccessLog.action == action)
        count_stmt = count_stmt.where(AuditAccessLog.action == action)
    if target_user_uid:
        stmt = stmt.where(AuditAccessLog.target_user_uid == target_user_uid)
        count_stmt = count_stmt.where(
            AuditAccessLog.target_user_uid == target_user_uid
        )

    stmt = stmt.order_by(AuditAccessLog.id.desc()).limit(limit).offset(offset)

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = list((await session.execute(stmt)).scalars().all())
        total = (await session.execute(count_stmt)).scalar() or 0

    return rows, total


# ==================== 按会话折叠（决策 #52 · C1） ====================


def _session_group_key():
    """一行 = 一个会话。没有 session_id 的记录退化成「一行一条」。

    ⚠️ **没有 session_id 不能从列表里消失**。stock Hermes、绕过我们客户端的
    直连调用、信封校验没过的，永远拿不到会话 ID —— 「分不了组就不显示」
    正好把最该被看见的那类调用藏起来了。所以给它们一个**必然唯一**的组键
    （`#<id>`），落回一行一条。

    前缀 `#` 不会和真的 session_id 撞：那一列入库前按
    `^[A-Za-z0-9_-]{1,64}$` 校验过，`#` 不在字符集里
    （见 services/audit_envelope.py）。
    """
    return func.coalesce(
        ConversationLog.session_id,
        func.concat("#", ConversationLog.id),
    )


async def fetch_sessions(
    start: date,
    end: date,
    limit: int,
    offset: int,
    **filters,
) -> Tuple[List, int]:
    """按会话分组的列表 + 会话总数

    一次提问会散成十几条记录（标题生成、N 个 agent 轮次、命令审批扫描），
    平铺出来彼此看不出关系。这里按 Hermes 的会话 ID 折叠成一行。

    ⚠️ **`GROUP BY` 里必须带 `user_uid`**，这是决策 #52 的第 2 条硬规矩：
    session_id 客户端可伪造，不带 user_uid 分组的话，填了别人 session_id 的
    记录会被并进别人的会话行里，看起来就像那个人问的。

    token 从 `usage_records` 关联：内容与指标**分表存放**（见 model/database.py），
    `conversation_logs` 上没有 token。两张表按 request_id 一对一。
    """
    group_key = _session_group_key().label("session_key")

    aggregates = [
        group_key,
        ConversationLog.user_uid,
        func.max(ConversationLog.session_id).label("session_id"),
        func.count(ConversationLog.id).label("turns"),
        func.min(ConversationLog.id).label("first_id"),
        func.max(ConversationLog.id).label("last_id"),
        func.min(ConversationLog.created_at).label("started_at"),
        func.max(ConversationLog.created_at).label("ended_at"),
        func.max(ConversationLog.dept_id).label("dept_id"),
        func.max(ConversationLog.role).label("role"),
        func.max(ConversationLog.model_name).label("model_name"),
        func.sum(ConversationLog.attachment_count).label("attachment_count"),
        # 「这段会话里有没有出过错 / 有没有被截断 / 有没有被兜底替换过」——
        # 聚合成一行之后，这三个标记只要有一条命中就该亮，所以取 max 而不是 min
        func.max(case((ConversationLog.status != "success", 1), else_=0)).label("has_failure"),
        func.max(case((ConversationLog.masked, 1), else_=0)).label("has_masked"),
        func.max(case((ConversationLog.truncated, 1), else_=0)).label("has_truncated"),
        func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("total_tokens"),
    ]

    stmt = select(*aggregates).select_from(ConversationLog).outerjoin(
        UsageRecord, UsageRecord.request_id == ConversationLog.request_id
    )
    stmt = _apply_filters(stmt, start, end, **filters)
    stmt = (
        stmt.group_by(group_key, ConversationLog.user_uid)
        # 按会话内最后一次调用排序，而不是按会话开始时间：
        # 刚刚还在聊的会话应该排在最前面
        .order_by(func.max(ConversationLog.id).desc())
        .limit(limit)
        .offset(offset)
    )

    # 总数是**会话数**不是记录数。子查询里只留分组键，外面数行数 ——
    # 直接 count(distinct) 在带 join 的语句上容易把 join 放大的行数一起数进去
    inner = select(group_key, ConversationLog.user_uid).select_from(ConversationLog)
    inner = _apply_filters(inner, start, end, **filters)
    inner = inner.group_by(group_key, ConversationLog.user_uid).subquery()
    count_stmt = select(func.count()).select_from(inner)

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(stmt)).all()
        total = (await session.execute(count_stmt)).scalar() or 0

    return list(rows), int(total)


async def fetch_session_titles(ids: List[int]) -> Dict[int, str]:
    """取每个会话首条记录的摘要，用作会话标题。

    单独一次查询而不是塞进聚合：`last_user_preview` 是 VARCHAR(512)，
    在 GROUP BY 里对它做 MIN/MAX 取到的是**字典序最小的那条**，不是最早那条 ——
    看起来能用，实际给出的标题和会话第一句话没有任何关系。
    """
    if not ids:
        return {}

    stmt = select(ConversationLog.id, ConversationLog.last_user_preview).where(
        ConversationLog.id.in_(ids)
    )

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(stmt)).all()

    return {row.id: (row.last_user_preview or "").strip() for row in rows}
