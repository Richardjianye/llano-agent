"""个人技能空间 —— 从界面把本机技能发到**本部门**的技能空间（阶段 22）

这是全新文件，不是从 web_server.py 抽出来的：上游没有「发布到公司技能库」这件事，
所以放进 skills.py 只会让每次月度同步都在那个文件上产生冲突。

## 为什么界面上也要有一个发布入口

发布本来只能在对话里说一句「把这个技能发到技能库」（skill_manage 的 publish 动作）。
那条路要求员工先想起来有这么回事。界面入口是给同一件事第二个触发点：员工在「技能」页
看着自己的技能列表时，正是他最可能想到「这个可以给同事用」的时刻。

2026-10-16 起界面只保留「整理并发布」（机器检查 → AI 整理 → 审阅后发布），去掉了
「原样发布」：Agent 习得技能时已按发布规范写（tools/skill_publish_spec.py），合规的技能
机器检查一过就能直接发，不合规的正需要整理这一步。

## 发布的范围是**部门**，不是全公司

网关在阶段 21 分了层：客户端发布一律进发布者所在部门的技能空间，晋级到公司空间
是主管在后台做的动作。这里不提供「发到哪儿」的选项 —— 能选的话，每个人都会直接
选公司空间，主管筛选那一步就形同虚设。

## 不吞异常

发布时静默失败是灾难 —— 员工以为自己交了这个月的技能指标，hub 上什么都没有。
所以这里把失败原封不动抛成 4xx/5xx，让界面能把网关那句话显示出来。
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.web_deps import late

_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

_config_profile_scope = late("_config_profile_scope")


# ---------------------------------------------------------------------------
# 「整理并发布」（2026-10-16，P2）—— 机器检查 → AI 整理 → 员工审阅后发布。
# 前两步只读且按技能内容缓存；发布一步服务端再查一遍敏感信息，没处理完就拒绝。
# ---------------------------------------------------------------------------

_profile_scope = late("_profile_scope")


class SkillPublishStepRequest(BaseModel):
    name: str
    # 员工点了「重新检查」：忽略按内容指纹缓存的上次结果
    refresh: bool = False
    profile: Optional[str] = None


class SkillRefinePublishRequest(BaseModel):
    name: str
    skill_md: str
    changelog: str = ""
    # 员工确认「这是公开信息、保留」的敏感项原文；其余命中必须已被替换掉。
    kept: list[str] = []
    replace_local: bool = True
    # 原 slug 被同事占用时，员工填的新发布标识；为空则沿用本机技能名。
    slug: Optional[str] = None
    profile: Optional[str] = None


async def _refine_call(profile, fn):
    def work():
        with _profile_scope(profile), _config_profile_scope(profile):
            return fn()
    try:
        return await asyncio.to_thread(work)
    except Exception as exc:
        from tools.llano_skill_hub import SkillHubUnavailable
        if isinstance(exc, SkillHubUnavailable):
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if isinstance(exc, (ValueError, OSError)):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _log.exception("skill refine failed")
        raise HTTPException(status_code=500, detail=f"整理发布失败：{exc}") from exc


@router.post("/api/skills/publish/check")
async def check_skill_publish(body: SkillPublishStepRequest, profile: Optional[str] = None):
    """第一步：机器检查（打包、安全扫描、发布规范、脱敏、网关预检），几秒内返回。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    from tools.skill_refine import check_publish
    return await _refine_call(body.profile or profile, lambda: check_publish(name, refresh=body.refresh))


@router.post("/api/skills/publish/refine")
async def refine_skill_draft(body: SkillPublishStepRequest, profile: Optional[str] = None):
    """第二步：AI 整理草稿（可能要几十秒）。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    from tools.skill_refine import refine_publish
    return await _refine_call(body.profile or profile, lambda: refine_publish(name, refresh=body.refresh))


@router.post("/api/skills/publish/refined")
async def publish_refined_skill(body: SkillRefinePublishRequest, profile: Optional[str] = None):
    name = (body.name or "").strip()
    if not name or not body.skill_md.strip():
        raise HTTPException(status_code=400, detail="name and skill_md are required")
    from tools.skill_refine import publish_refined
    result = await _refine_call(body.profile or profile, lambda: publish_refined(
        name, body.skill_md, changelog=(body.changelog or "").strip(), kept=body.kept,
        replace_local=body.replace_local, slug=(body.slug or "").strip() or None))
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "发布失败")
    return result


class SkillShareHintRequest(BaseModel):
    name: str
    profile: Optional[str] = None


@router.post("/api/skills/publish/dismiss-suggestion")
async def dismiss_share_suggestion(body: SkillShareHintRequest, profile: Optional[str] = None):
    """「值得分享」提示点了「不再提示」。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    from tools.skill_refine import dismiss_share_suggestion as dismiss
    await _refine_call(body.profile or profile, lambda: dismiss(name))
    return {"ok": True}
