"""公司系统提示词策略管理。

正文只进入草稿/版本响应；操作日志仅记版本、revision 与 hash。
"""

from fastapi import APIRouter, Depends
from loguru import logger

from model.request.prompt_policy import (
    PromptPolicyDraftSave,
    PromptPolicyPublish,
    PromptPolicyRollback,
)
from model.response import Response, UserInfo
from services.prompt_policy import (
    get_admin_state,
    get_history,
    publish_draft,
    rollback,
    save_draft,
)
from utils.dependencies import require_permission


router = APIRouter(prefix="/prompt-policy", tags=["提示词策略"])


@router.get("", response_model=Response)
async def prompt_policy_state(
    _: UserInfo = Depends(require_permission("prompt_policy:view")),
):
    return Response(data=await get_admin_state())


@router.put("/draft", response_model=Response)
async def prompt_policy_draft_save(
    body: PromptPolicyDraftSave,
    operator: UserInfo = Depends(require_permission("prompt_policy:edit")),
):
    draft = await save_draft(
        company_core_policy=body.company_core_policy,
        default_persona=body.default_persona,
        expected_revision=body.expected_revision,
        actor=operator,
    )
    logger.info(
        f"提示词策略草稿已保存 revision={draft['revision']} "
        f"hash={draft['content_hash']} by={operator.uid}"
    )
    return Response(data={"draft": draft})


@router.post("/publish", response_model=Response)
async def prompt_policy_publish(
    body: PromptPolicyPublish,
    operator: UserInfo = Depends(require_permission("prompt_policy:publish")),
):
    published = await publish_draft(
        expected_revision=body.expected_revision, actor=operator
    )
    logger.info(
        f"提示词策略已发布 version={published['version']} "
        f"hash={published['content_hash']} by={operator.uid}"
    )
    return Response(data={"published": published})


@router.get("/history", response_model=Response)
async def prompt_policy_history(
    _: UserInfo = Depends(require_permission("prompt_policy:view")),
):
    return Response(data=await get_history())


@router.post("/rollback/{target_version}", response_model=Response)
async def prompt_policy_rollback(
    target_version: int,
    body: PromptPolicyRollback,
    operator: UserInfo = Depends(require_permission("prompt_policy:publish")),
):
    published = await rollback(
        target_version=target_version,
        expected_current_version=body.expected_current_version,
        actor=operator,
    )
    logger.warning(
        f"提示词策略已回滚 new_version={published['version']} "
        f"source_version={target_version} hash={published['content_hash']} "
        f"by={operator.uid}"
    )
    return Response(data={"published": published})
