"""Canonical client redaction policy published by Audit Gateway."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

POLICY_TTL_SECONDS = 300


def normalized_keywords(raw: str) -> list[str]:
    by_casefold: dict[str, str] = {}
    for item in raw.split(","):
        keyword = item.strip()
        if keyword:
            by_casefold.setdefault(keyword.casefold(), keyword)
    return sorted(by_casefold.values(), key=lambda value: (-len(value), value.casefold()))


SCOPE_GLOBAL = "global"
SCOPE_DEPT = "dept"


async def keywords_for(dept_id: int | None, *, strict: bool = False) -> list[str]:
    """这个部门实际生效的词表 = 环境变量基线 ∪ global 行 ∪ 本部门及所有上级的行。

    **相加不是替换。** 环境变量那份继续作为全局基线，否则某天有人建了第一条
    dept 规则，全局那几个词当场失效，而没有任何一处会报错。

    读不到数据库时**退回环境变量那份**而不是空表：缓存/库挂了应该让防线降级到
    基线，不是整个关掉。遮多了是噪音，遮漏了是泄漏。

    ## ⚠️ `scope_value` 存的是部门 **ID**，不是部门 code

    部门从管理后台下拉选择，使用当前数据库中的 ID，不能填写部门 code。

    ## 子部门继承上级的词

    方向与技能空间 / 配额一致（`services/org_tree.expand_ancestors`）。
    本部门及所有上级的规则取并集，避免子部门遗漏上级配置的屏蔽词。
    """
    from config import get_settings

    raw = get_settings().mask_keywords or ""
    collected = [k for k in raw.split(",") if k.strip()]

    try:
        from db import init_async_session
        from model.database import RedactionKeyword
        from services.org_tree import dept_ancestor_ids
        from sqlalchemy import or_, select

        conditions = [RedactionKeyword.scope == SCOPE_GLOBAL]
        if dept_id is not None:
            chain = await dept_ancestor_ids(dept_id) or (int(dept_id),)
            conditions.append(
                (RedactionKeyword.scope == SCOPE_DEPT)
                & RedactionKeyword.scope_value.in_([str(i) for i in chain])
            )

        maker = await init_async_session()
        async with maker() as session:
            rows = (
                await session.execute(
                    select(RedactionKeyword.keyword).where(
                        RedactionKeyword.status == 1, or_(*conditions)
                    )
                )
            ).scalars().all()
        collected.extend(rows)
    except Exception as exc:  # noqa: BLE001 — 库挂了要降级到基线，不是关掉防线
        if strict:
            raise RuntimeError("无法读取发送方向屏蔽词策略") from exc
        from loguru import logger

        logger.warning(f"屏蔽词表读不到，退回 MASK_KEYWORDS 基线：{exc}")

    # normalized_keywords 负责去重（按 casefold）与长词优先排序 ——
    # 「示例公司科技」必须排在「示例公司」前面，否则短词先匹配会把长词切成
    # 「[公司]科技」，漏掉更具体的那半截。
    return normalized_keywords(",".join(collected))


def build_redaction_policy(
    *, now: datetime | None = None, keywords: list[str] | None = None
) -> dict[str, Any]:
    """Build a short-lived policy whose version changes only with semantics.

    `keywords` 由调用方传入（`keywords_for(dept_id)` 的结果）。不传时退回环境
    变量那份 —— 保住既有调用方与测试，也保证这个函数本身仍是纯的、可单测。

    **version 会随部门不同而不同**，这是对的：客户端按 version 缓存，两个部门
    拿到同一个 version 却是不同词表的话，换个人登录就会用错词表。
    """
    from config import get_settings

    settings = get_settings()
    if keywords is None:
        keywords = normalized_keywords(settings.mask_keywords or "")
    placeholder = settings.mask_placeholder or "[公司]"
    semantic_payload = {
        "keywords": sorted((keyword.casefold() for keyword in keywords), key=lambda value: (-len(value), value)),
        "placeholder": placeholder,
    }
    digest = hashlib.sha256(
        json.dumps(semantic_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    issued_at = now or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    expires_at = issued_at.astimezone(timezone.utc) + timedelta(
        seconds=POLICY_TTL_SECONDS
    )

    return {
        "object": "llano.redaction_policy",
        "version": f"sha256:{digest}",
        "keywords": keywords,
        "placeholder": placeholder,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
