"""自有 skill hub 的管理端 —— 阶段 11 · S11-G

写入侧在 router/v1/skill_hub.py（员工发布）；这里做两件事：**下架/恢复**，
和**技能指标统计**。

## 流程是先发后审

发布即全公司可见，管理员事后可下架。跟技能安全那一套同一个取向：不拦人、留痕、
事后处理。审核队列会卡住技能指标，而指标正是这东西存在的理由。

所以这个页面的日常用途不是审批，是两个别的：
1. 出问题时把一个技能从全公司的可安装列表里摘掉
2. 看各部门这个月做了多少技能（这是 hub 的产品目的本身）

## 统计给两个数：技能数与版本数

把一个技能迭代五版也是五次「总结工作经验」，只数技能会让持续打磨的人看起来
不如到处新建的人。两个数都给出去，让看报表的人自己决定按哪个考核 ——
这是个管理决策，不该由接口替他定死。

## 权限拆成 view / edit

下架等于把一个技能从全公司的可安装列表里摘掉，与推一个安装包同级，所以
`skill_hub:edit` 照 `release:edit` 的名单。看列表和数字则宽一档。
"""

from __future__ import annotations

from typing import Optional

from db.selects.skill_hub import (admin_list_skills, count_skills_by_dept,
                                  count_skills_by_publisher,
                                  get_version_files, list_versions)
from db.updates.skill_hub import set_skill_scope, set_skill_status
from fastapi import APIRouter, Body, Depends, Query
from loguru import logger
from model.database import ROLE_DEVELOPER, ROLE_PRESIDENT
from model.response import Response, UserInfo
from services.org_tree import dept_scope_ids
from services.skill_hub import (DELISTED, LISTED, SCOPE_COMPANY, SCOPE_DEPT,
                                can_browse_all, can_promote)
from utils.dependencies import require_permission
from utils.errors import NotFoundError, PermissionError_

router = APIRouter()


@router.get("/skill-hub/list", response_model=Response)
async def skill_hub_list(
    status: str = Query("", description="listed / delisted，空为全部"),
    dept_id: Optional[int] = Query(None),
    keyword: str = Query("", description="匹配 slug / 名称 / 发布者"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    operator: UserInfo = Depends(require_permission("skill_hub:view")),
):
    """技能列表。**含已下架** —— 员工来问「我搜不到 xxx 了」时后台得答得上来。

    ⚠️ 主管只看得到**自己部门空间**里的（阶段 21）。范围由服务端按角色算，
    不是页面上的筛选框 —— 后者是给人用的，前者是不能绕过的。
    """
    items, total = await admin_list_skills(
        status=status,
        dept_id=dept_id,
        keyword=keyword,
        page=page,
        page_size=page_size,
        restrict_to_depts=None if can_browse_all(operator.permission, operator.dept_code) else await dept_scope_ids(operator.dept_id),
    )
    return Response(data={"items": items, "total": total, "page": page})


@router.get("/skill-hub/versions", response_model=Response)
async def skill_hub_versions(
    slug: str = Query(..., description="技能的安装标识"),
    _: UserInfo = Depends(require_permission("skill_hub:view")),
):
    """一个技能的版本历史。不含文件内容 —— 见 db/selects 里的说明。"""
    return Response(data={"versions": await list_versions(slug.strip().lower())})


@router.get("/skill-hub/files", response_model=Response)
async def skill_hub_files(
    slug: str = Query(..., description="技能的安装标识"),
    version: Optional[int] = Query(None, description="不传则取当前该装的那一版"),
    _: UserInfo = Depends(require_permission("skill_hub:view")),
):
    """审阅一个技能包的内容（2026-09-03）。

    ## 为什么加这条

    在这之前，后台能看到技能的名字、发布者、版本数，唯独看不到**它到底是什么**。
    要决定下不下架，只能凭技能名猜，或者自己去客户端装一份 —— 而下架恰恰是这个
    页面唯一的止血手段。技能安全那一页早就能读样本源码了，同样一件事在技能库
    这边缺着，是漏做，不是设计。

    ## 权限只要 `skill_hub:view`，不像样本那样再叠一个点

    技能安全的样本是**被隔离的**代码：员工自己写的、还没打算给别人看的东西，
    所以那边额外要 `skill_security:sample`，只给能读代码的人。

    这里是**已发布**的技能包 —— 公司里任何人都能在客户端里装下来读，它的存在
    目的就是被别人用。给它加一道后台才有的门禁，挡不住任何人，只会让下架决策
    继续靠猜。

    ⚠️ 与列表分开是有意的（见 db/selects.get_version_files）：列表一页几十个
    技能包全带内容，等于把全公司的技能源码一次性推给任何打得开这个页面的人。
    """
    payload = await get_version_files(slug.strip().lower(), version)
    if payload is None:
        raise NotFoundError("技能或该版本不存在")
    return Response(data=payload)


@router.post("/skill-hub/delist", response_model=Response)
async def skill_hub_delist(
    skill_id: int = Body(..., embed=True),
    reason: str = Body("", embed=True),
    operator: UserInfo = Depends(require_permission("skill_hub:edit")),
):
    """下架。技能对客户端立刻彻底不存在（搜不到、装不了、发不了新版）。

    **不删数据**：已经装到员工机器上的那些不会因为下架而消失，留着这一行才能
    回答「这个技能当时是谁发的、为什么被下掉」。
    """
    result = await set_skill_status(
        skill_id, status=DELISTED, operator_uid=operator.uid, reason=reason
    )
    if result is None:
        raise NotFoundError("技能不存在")

    # 这一行是要被人看的：下架意味着有个全公司可安装的东西出了问题。
    logger.warning(
        f"skill hub 下架：{result['slug']}（操作人 {operator.username or operator.uid}，"
        f"原因：{reason.strip() or '未填'}）"
    )
    return Response(data=result)


@router.post("/skill-hub/relist", response_model=Response)
async def skill_hub_relist(
    skill_id: int = Body(..., embed=True),
    operator: UserInfo = Depends(require_permission("skill_hub:edit")),
):
    """恢复上架。会把下架痕迹清掉，否则列表上「已上架」和一条下架原因会同时显示。"""
    result = await set_skill_status(skill_id, status=LISTED, operator_uid=operator.uid)
    if result is None:
        raise NotFoundError("技能不存在")

    logger.info(
        f"skill hub 恢复上架：{result['slug']}（操作人 {operator.username or operator.uid}）"
    )
    return Response(data=result)


@router.get("/skill-hub/stats", response_model=Response)
async def skill_hub_stats(
    limit: int = Query(50, ge=1, le=200, description="按人统计返回多少条"),
    _: UserInfo = Depends(require_permission("skill_hub:view")),
):
    """技能指标：按部门、按人各一份。

    ⚠️ 这里的 skill_count / version_count 是**累计**，不是本月。

      按月切分要先定「月」的口径（服务器本地月，与用量报表一致），而报表口径
      是个会被拿去考核的东西，定错了比没有更糟。第一期先把累计数给出来 ——
      看得见谁在发、发了多少，已经能回答「这个功能有没有人用」。
      按月统计已记 TODO。
    """
    return Response(
        data={
            "by_dept": await count_skills_by_dept(),
            "by_publisher": await count_skills_by_publisher(limit),
        }
    )


# 能看到全部部门空间的角色。主管只看自己那个。
_ADMIN_ROLES = frozenset({ROLE_PRESIDENT, ROLE_DEVELOPER})


@router.post("/skill-hub/promote", response_model=Response)
async def skill_hub_promote(
    skill_id: int = Body(..., embed=True),
    operator: UserInfo = Depends(require_permission("skill_hub:promote")),
):
    """把一个技能标记为「公司精选」—— 阶段 21，2026-09-03 改语义。

    ⚠️ **这不再是一道可见性开关。** 技能发布即全员可见（见
    db/selects/skill_hub._visible_to），晋级现在只做两件事：排在技能库最前面，
    以及从原部门分组里单独归到「公司精选」。

    原本它是「让别人也能看到」的唯一途径，而那道关卡在发布初期卡得太死 ——
    技能做出来得等主管点一下才有第二个人看得见，主管多数时候并不知道有东西
    在等他。

    主管只能晋级**自己部门**的。这个判定在服务端做，不靠前端藏按钮。
    """
    items, _ = await admin_list_skills(page_size=100, keyword="")
    target = next((s for s in items if s["id"] == skill_id), None)
    if target is None:
        raise NotFoundError("技能不存在")

    if not can_promote(
        operator.permission, operator.dept_id, target.get("scope_dept_id"),
        is_admin=operator.permission in _ADMIN_ROLES,
    ):
        raise PermissionError_("只能把自己部门的技能发到公司空间")

    result = await set_skill_scope(skill_id, scope=SCOPE_COMPANY, operator_uid=operator.uid)
    if result is None:
        raise NotFoundError("技能不存在")

    # 精选是推荐，不是开关；用 info 而不是 warning —— 它不再改变任何人的可见范围。
    logger.info(
        f"技能标记为公司精选：{result['slug']}（操作人 {operator.username or operator.uid}）"
    )
    return Response(data=result)


@router.post("/skill-hub/withdraw", response_model=Response)
async def skill_hub_withdraw(
    skill_id: int = Body(..., embed=True),
    operator: UserInfo = Depends(require_permission("skill_hub:promote")),
):
    """从公司空间撤回部门空间。

    这是主管手上的工具，与下架（`skill_hub:edit`，管理部门）不同：**撤回是降级，
    下架是否决**。技能撤回后在原部门仍然可用；下架之后谁都装不了。

    退回的目标部门用技能自己的 `owner_dept_id`，不是操作人的部门 —— 操作人可能是
    管理部门的人，把技能退回到管理部门显然不对。
    """
    result = await set_skill_scope(skill_id, scope=SCOPE_DEPT, operator_uid=operator.uid)
    if result is None:
        raise NotFoundError("技能不存在")

    logger.info(
        f"技能撤回部门空间：{result['slug']} → 部门 {result.get('scope_dept_id')}"
        f"（操作人 {operator.username or operator.uid}）"
    )
    return Response(data=result)
