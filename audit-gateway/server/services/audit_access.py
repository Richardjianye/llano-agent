"""会话查阅日志 —— 「看的人也被看」

决策 #8 的原话是「管理部门查看行为本身也要记审计日志」，同时它也是第 5 节
合规动作里的一条。**做查询接口而不做这个，等于只完成了一半。**

## 为什么写在服务层而不是中间件里

中间件只知道「有人调了 GET /admin/audit/detail/123」，不知道这条会话属于谁。
而审计日志真正要回答的问题是「谁看了**谁的**内容」——归属人 uid 必须由
路由拿到数据之后才填得进去。所以记录点放在服务层，由各个接口显式调用。

## 四种颗粒度

选「列表 + 详情抽屉」方案（而不是表格直接展开全文）就是为了这张表能记细：

- `list`：只看到摘要，记查询条件与命中条数，不记具体是哪些人
- `detail`：展开了某一条全文 → 精确到「看了谁的哪一条」
- `attachment`：下载/预览了某个附件 → 附件是最敏感的部分，单独一条
- `export`：导出 CSV → 数据离开系统的时刻，最该留痕

如果当初选了「表格直接展开全文」，列表接口就得下发全文，
`detail` 这一行永远只能写成「看了第 3 页」，事后追责时基本没用。

## 失败不阻断查询

写日志失败只打 error 不抛异常 —— 让审计员因为日志表满了而查不了会话，
反而更糟。但**必须留 error 日志**，否则日志静默丢失比没有日志更危险。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

ACTION_LIST = "list"
ACTION_DETAIL = "detail"
ACTION_ATTACHMENT = "attachment"
ACTION_EXPORT = "export"

ALL_ACTIONS = (ACTION_LIST, ACTION_DETAIL, ACTION_ATTACHMENT, ACTION_EXPORT)

ACTION_LABELS = {
    ACTION_LIST: "检索列表",
    ACTION_DETAIL: "查看全文",
    ACTION_ATTACHMENT: "查看附件",
    ACTION_EXPORT: "导出 CSV",
}

# 查询条件 JSON 的长度上限。关键词理论上可以很长，
# 但存进来是为了事后分析行为模式，不是为了复现请求
_QUERY_MAX_CHARS = 2000


@dataclass
class AccessContext:
    """一次查阅的现场信息

    actor 的角色与部门存快照：这个人半年后被调离管理部门，
    也不该影响「他当时确实有权限查阅」这个事实的还原。
    """

    actor_uid: str
    actor_role: Optional[str] = None
    actor_dept_id: Optional[int] = None
    request_id: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None


def _dump_query(query: Optional[Dict[str, Any]]) -> Optional[str]:
    if not query:
        return None
    # 空值不入库：一堆 null 会淹没真正被用过的筛选条件
    cleaned = {k: v for k, v in query.items() if v not in (None, "", [])}
    if not cleaned:
        return None
    text = json.dumps(cleaned, ensure_ascii=False, default=str)
    return text[:_QUERY_MAX_CHARS]


async def log_access(
    ctx: AccessContext,
    action: str,
    *,
    conversation_id: Optional[int] = None,
    attachment_id: Optional[int] = None,
    target_user_uid: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
    result_count: int = 0,
) -> None:
    """记一条查阅日志。失败只告警，不影响调用方"""
    try:
        from sqlalchemy import insert

        from db import init_async_session
        from model.database import AuditAccessLog

        session_maker = await init_async_session()
        async with session_maker() as session:
            await session.execute(
                insert(AuditAccessLog).values(
                    actor_uid=ctx.actor_uid,
                    actor_role=ctx.actor_role,
                    actor_dept_id=ctx.actor_dept_id,
                    action=action,
                    target_conversation_id=conversation_id,
                    target_attachment_id=attachment_id,
                    target_user_uid=target_user_uid,
                    query_json=_dump_query(query),
                    result_count=result_count,
                    request_id=ctx.request_id,
                    client_ip=ctx.client_ip,
                    # UA 可能很长（尤其 Electron 客户端），截到列宽
                    user_agent=(ctx.user_agent or "")[:255] or None,
                )
            )
            await session.commit()
    except Exception as exc:
        # 不 raise：审计员不该因为日志表出问题而查不了会话。
        # 但这条 error 必须留下，静默丢日志比没有日志更危险
        logger.error(
            f"写会话查阅日志失败 actor={ctx.actor_uid} action={action} "
            f"conversation={conversation_id}: {exc}"
        )
