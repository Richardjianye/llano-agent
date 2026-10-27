"""桌面客户端读取当前已发布提示词策略。"""

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from model.response import Response as ApiResponse
from services.prompt_policy import get_effective_policy
from utils.gateway_auth import Caller, require_caller


router = APIRouter()

PROMPT_POLICY_UNPUBLISHED_CODE = 10011
PROMPT_POLICY_UNPUBLISHED_REASON = "prompt_policy_unpublished"


@router.get("/prompt-policy/effective")
async def effective_prompt_policy(
    response: Response,
    _: Caller = Depends(require_caller),
):
    """返回当前发布快照；空库用 404 明确要求客户端回退安装包基线。"""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization, Cookie"

    published = await get_effective_policy()
    if published is None:
        # 直接构造响应而不是抛 AppError：异常处理器会新建一个 JSONResponse，丢掉
        # 上面的 no-store。404 一旦被代理缓存，首版发布后客户端仍会长期走基线。
        return JSONResponse(
            status_code=404,
            content=ApiResponse(
                code=PROMPT_POLICY_UNPUBLISHED_CODE,
                message="网关尚未发布提示词策略，请使用安装包内置基线",
                data={
                    "reason": PROMPT_POLICY_UNPUBLISHED_REASON,
                    "fallback": "bundled",
                },
            ).model_dump(),
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Vary": "Authorization, Cookie",
            },
        )

    # 契约刻意只含客户端需要的五个字段。操作者与 source_version 只在管理端可见。
    return {
        "version": published["version"],
        "content_hash": published["content_hash"],
        "company_core_policy": published["company_core_policy"],
        "default_persona": published["default_persona"],
        "published_at": published["published_at"],
    }
