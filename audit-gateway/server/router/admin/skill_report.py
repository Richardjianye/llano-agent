"""技能安全事件查看 —— 阶段 7 · S7-C（管理端只读）

写入侧在 router/v1/skill_reports.py（客户端上报）；这里只负责把已经落库的
事件切出来给人看。

## 这张表读的是什么

产品前提：技能来源**不封禁**。自有 skill hub 是给各部门下技能指标用的，
把员工的工作经验转成公司资产。所以这里不是「拦截报表」，是**越过报表** ——
扫描器判定 dangerous 会自动隔离，但员工可以自己放回去，代价是留一行。

看这个页面的人要回答的问题是：
1. 这条隔离是不是误报？（要读样本代码）
2. 同一个技能包，有多少人把它放回去了？（靠 bundle_hash 串）
3. 发布前扫描命中后，哪份技能包已经发布、需要线下联系谁？

## 权限拆成两个点

- `skill_security:view`：列表与事件元数据。合规与部门指标那一侧的事。
- `skill_security:sample`：**员工自己写的源码**。判断一段 curl 是外发密钥还是
  正常调接口是工程判断，交给能读代码的人。

列表接口一律不下发样本内容，哪怕调用方两个权限都有 —— 一页四十条全带上，
等于把几十个人的代码一次性推给任何打得开这个页面的人。

## 本期只做上行

复核结论怎么回到客户端是下一期的事，所以这里没有任何「标记已审」的写接口。
"""

from __future__ import annotations

import json
from typing import Optional

from db.selects.skill_report import (fetch_event, fetch_events,
                                     fetch_related_events, fetch_sample)
from fastapi import APIRouter, Depends, Query
from model.response import Response, UserInfo
from utils.dependencies import require_permission
from utils.errors import NotFoundError, PermissionError_

router = APIRouter()


@router.get("/skill-events/list", response_model=Response)
async def skill_event_list(
    event: str = Query("", description="skill_quarantined / skill_quarantine_restored / skill_publish_warning"),
    verdict: str = Query("", description="dangerous / caution"),
    dept_id: Optional[int] = Query(None),
    user_uid: str = Query(""),
    skill_name: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: UserInfo = Depends(require_permission("skill_security:view")),
):
    """事件列表。**不含样本内容**，见模块说明。"""
    items, total = await fetch_events(
        event=event.strip() or None,
        verdict=verdict.strip() or None,
        dept_id=dept_id,
        user_uid=user_uid.strip() or None,
        skill_name=skill_name.strip() or None,
        page=page,
        page_size=page_size,
    )
    return Response(data={"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/skill-events/detail/{event_row_id}", response_model=Response)
async def skill_event_detail(
    event_row_id: int,
    operator: UserInfo = Depends(require_permission("skill_security:view")),
):
    """事件详情：命中项明细 + 同一技能包的其它事件。

    样本本体不在这里，在 /skill-events/sample/{id} —— 那道口子要另一个权限点，
    而且分开之后「取代码」是一个单独的动作，将来要加查阅日志有地方挂。
    """
    row = await fetch_event(event_row_id)
    if row is None:
        raise NotFoundError("事件不存在")

    findings = []
    if row.findings_json:
        try:
            findings = json.loads(row.findings_json)
        except ValueError:
            # 一条坏 JSON 不该让整个详情页打不开：其余字段照样是有用的证据。
            findings = []

    return Response(
        data={
            "id": row.id,
            "event": row.event,
            "event_id": row.event_id,
            "user_uid": row.user_uid,
            "dept_id": row.dept_id,
            "dept_code": row.dept_code,
            "device": row.device,
            "skill_name": row.skill_name,
            "entry_id": row.entry_id,
            "verdict": row.verdict,
            "reason": row.reason,
            "source": row.source,
            "origin_relpath": row.origin_relpath,
            "scanner_version": row.scanner_version,
            "bundle_hash": row.bundle_hash,
            "finding_count": row.finding_count,
            "findings": findings,
            "sample_id": row.sample_id,
            "occurred_at": row.occurred_at,
            "reported_at": row.reported_at,
            "created_at": row.created_at,
            "related": await fetch_related_events(row.bundle_hash or "", exclude_id=row.id),
        }
    )


@router.get("/skill-events/sample/{sample_id}", response_model=Response)
async def skill_event_sample(
    sample_id: int,
    operator: UserInfo = Depends(require_permission("skill_security:view")),
):
    """样本本体 —— 员工写的源码。

    权限在函数体里再判一次 `skill_security:sample`，而不是写在依赖项上：
    依赖项只能声明一个权限点，而这条路径要求「能进这个页面」**并且**
    「能读代码」两条同时成立。
    """
    from services.permissions import has_permission

    if not has_permission(operator.permission, "skill_security:sample", operator.dept_code):
        raise PermissionError_("没有查看技能样本源码的权限")

    row = await fetch_sample(sample_id)
    if row is None:
        raise NotFoundError("样本不存在")

    try:
        files = json.loads(row.files_json)
    except ValueError:
        files = []

    return Response(
        data={
            "id": row.id,
            "sample_hash": row.sample_hash,
            "bundle_hash": row.bundle_hash,
            "file_count": row.file_count,
            "included_count": row.included_count,
            "included_bytes": row.included_bytes,
            # 有文件因为超限或是二进制没传上来时，这两个字段是给审查的人看的提示：
            # 「你现在看到的不是全部」。缺了它，读到一份看着人畜无害的样本就会
            # 得出「误报」的结论，而真正的东西在那个没传上来的 12MB 文件里。
            "truncated": row.truncated,
            "truncated_reason": row.truncated_reason,
            "files": files,
            "seen_count": row.seen_count,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
        }
    )
