"""屏蔽词管理 —— 阶段 18

形状照 router/admin/quota.py：`scope` + `scope_value`，全局与部门两档。照抄是
有意的 —— 后台已经有两个「按部门配」的页面，第三个长得不一样只会让人每次都要
重新读一遍规则。

## ⚠️ 改这里是安全配置，不是内容配置

删一个词等于当场把那条防线关掉，而且**已经发出去的消息不会追溯遮蔽** ——
群聊消息的脱敏发生在写入那一刻，库里存的就是当时的结果。所以：

- 权限与 `model:edit` 同档（总裁 / 开发者）
- 删除走**停用**（status=0）而不是物理删，留着才能回答「这个词什么时候不再遮了」
"""

from __future__ import annotations

from typing import Optional

from db import init_async_session
from fastapi import APIRouter, Body, Depends, Query
from loguru import logger
from model.database import RedactionKeyword, now
from model.response import Response, UserInfo
from services.redaction_policy import SCOPE_DEPT, SCOPE_GLOBAL, normalized_keywords
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from utils.dependencies import require_permission
from utils.errors import NotFoundError, ValidationError_

router = APIRouter()


def _payload(r: RedactionKeyword) -> dict:
    return {
        "id": int(r.id),
        "scope": r.scope,
        "scope_value": r.scope_value or "",
        "keyword": r.keyword,
        "status": int(r.status),
        "remark": r.remark or "",
        "updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else "",
    }


@router.get("/redaction/list", response_model=Response)
async def redaction_list(
    scope: str = Query("", description="global / dept，空为全部"),
    _: UserInfo = Depends(require_permission("redaction:edit")),
):
    maker = await init_async_session()
    async with maker() as session:
        stmt = select(RedactionKeyword)
        if scope.strip():
            stmt = stmt.where(RedactionKeyword.scope == scope.strip())
        rows = (
            await session.execute(stmt.order_by(RedactionKeyword.scope, RedactionKeyword.id))
        ).scalars().all()
    return Response(data={"items": [_payload(r) for r in rows]})


@router.get("/redaction/effective", response_model=Response)
async def redaction_effective(
    dept_id: Optional[int] = Query(None, description="不传则只看全局"),
    _: UserInfo = Depends(require_permission("redaction:edit")),
):
    """某个部门**实际生效**的词表。

    这条不是冗余：生效词表 = 环境变量基线 ∪ global 行 ∪ 本部门及所有上级的行。
    只看列表页的话，配的人永远不知道最终是哪几个词、按什么顺序匹配 ——
    而顺序是有意义的（长词优先，否则「示例公司科技」会被切成「[公司]科技」）。

    2026-09-03 之后这条更是唯一能一眼看出「环境变量里还有没有残留」的地方：
    不传 dept_id 拿到的就是纯基线，非空即说明 `MASK_KEYWORDS` 还配着东西，
    那份是**对所有部门无条件生效**的。
    """
    from services.redaction_policy import keywords_for

    return Response(data={"keywords": await keywords_for(dept_id)})


@router.post("/redaction/save", response_model=Response)
async def redaction_save(
    keyword: str = Body(..., embed=True),
    scope: str = Body(SCOPE_GLOBAL, embed=True),
    scope_value: str = Body("", embed=True),
    remark: str = Body("", embed=True),
    rule_id: Optional[int] = Body(None, embed=True),
    status: int = Body(1, embed=True),
    operator: UserInfo = Depends(require_permission("redaction:edit")),
):
    word = (keyword or "").strip()
    if not word:
        raise ValidationError_("屏蔽词不能为空")
    if len(word) > 128:
        raise ValidationError_("屏蔽词超过 128 字符")
    if scope not in {SCOPE_GLOBAL, SCOPE_DEPT}:
        raise ValidationError_("scope 只能是 global 或 dept")
    if scope == SCOPE_DEPT and not (scope_value or "").strip():
        raise ValidationError_("按部门配置时必须指定部门")
    value = (scope_value or "").strip() if scope == SCOPE_DEPT else ""

    maker = await init_async_session()
    async with maker() as session:
        if rule_id:
            result = await session.execute(
                update(RedactionKeyword).where(RedactionKeyword.id == rule_id)
                .values(keyword=word, scope=scope, scope_value=value,
                        remark=(remark or "").strip()[:255] or None,
                        status=1 if status else 0, updated_at=now())
            )
            if result.rowcount == 0:
                raise NotFoundError("规则不存在")
            await session.commit()
            saved = rule_id
        else:
            try:
                created = await session.execute(
                    insert(RedactionKeyword).values(
                        scope=scope, scope_value=value, keyword=word,
                        status=1 if status else 0,
                        remark=(remark or "").strip()[:255] or None,
                        created_at=now(), updated_at=now())
                )
                await session.commit()
                saved = int(created.inserted_primary_key[0])
            except IntegrityError:
                await session.rollback()
                raise ValidationError_("这个作用域下已经有同样的词了")

    logger.info(
        f"屏蔽词变更：{scope}/{value or '-'} 「{word}」"
        f"（{'启用' if status else '停用'}，操作人 {operator.username or operator.uid}）"
    )
    return Response(data={"id": saved})


@router.post("/redaction/disable", response_model=Response)
async def redaction_disable(
    rule_id: int = Body(..., embed=True),
    operator: UserInfo = Depends(require_permission("redaction:edit")),
):
    """停用一个词。**不物理删** —— 留着才能回答「这个词什么时候不再遮了」。

    ⚠️ 停用**不会**让已经发出去的消息重新显示原文：群聊消息的脱敏发生在写入那
    一刻，库里存的就是当时的结果。这是对的（历史不该被改写），但配的人要知道。
    """
    maker = await init_async_session()
    async with maker() as session:
        result = await session.execute(
            update(RedactionKeyword).where(RedactionKeyword.id == rule_id)
            .values(status=0, updated_at=now())
        )
        if result.rowcount == 0:
            raise NotFoundError("规则不存在")
        await session.commit()

    logger.warning(
        f"屏蔽词停用：#{rule_id}（操作人 {operator.username or operator.uid}）"
        f" —— 这条防线对**之后**的消息失效，已发出的不追溯"
    )
    return Response(data={"id": rule_id})
