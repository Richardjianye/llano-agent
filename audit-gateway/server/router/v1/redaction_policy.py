"""Authenticated policy endpoint consumed by branded Hermes runtimes.

CUSTOM（阶段 18）：词表按**调用者所属部门**解析 —— 全局基线 ∪ global 行 ∪
本部门及所有上级部门的行。形状照 model_whitelist / token_quotas，见
model/database.RedactionKeyword。
"""

from fastapi import APIRouter, Depends, Response

from services.redaction_policy import build_redaction_policy, keywords_for
from utils.gateway_auth import Caller, require_caller

router = APIRouter()


@router.get("/redaction-policy")
async def redaction_policy(
    response: Response,
    caller: Caller = Depends(require_caller),
):
    # no-store：词表改了要立刻生效。中间层缓存住等于把旧词表钉死，
    # 而「某个词改完之后还是没遮」是这类问题里最难查的一种。
    response.headers["Cache-Control"] = "no-store"
    # 部门从 API Key / JWT 解出来，不听客户端 —— 否则谁都能报一个词表最短的
    # 部门来少遮几个词。
    return build_redaction_policy(keywords=await keywords_for(caller.dept_id))
