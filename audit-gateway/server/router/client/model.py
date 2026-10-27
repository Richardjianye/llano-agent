"""客户端模型清单

客户端被锁死为只能使用网关下发的模型（决策 #7），这里是唯一的模型来源。
返回内容不含上游地址与 API Key。
"""

from db.selects.user import get_user_info
from fastapi import APIRouter, Depends
from model.response import Response, UserInfo
from services.image_generation import is_image_model
from services.model_access import (AccessSubject, list_available_models,
                                   models_to_payload)
from utils.dependencies import get_access_subject, require_permission
from utils.errors import NotFoundError

router = APIRouter(tags=["模型清单"])


@router.get("/models", response_model=Response)
async def my_models(subject: AccessSubject = Depends(get_access_subject)):
    """当前用户可用的模型列表

    白名单为默认拒绝：没有任何授权时返回空列表，客户端应提示联系管理员开通。
    """
    models = await list_available_models(subject)
    return Response(data={"items": models_to_payload(models)})


@router.get("/models/preview/{uid}", response_model=Response)
async def preview_user_models(
    uid: str,
    _: UserInfo = Depends(require_permission("model:view")),
):
    """管理端预览指定用户的最终可用模型 —— 配完白名单后自查用"""
    user = await get_user_info(uid=uid)
    if not user:
        raise NotFoundError("用户不存在")

    subject = AccessSubject(
        uid=user.uid, role=user.permission, dept_id=user.dept_id
    )
    models = await list_available_models(subject)
    return Response(
        data={
            "user": {
                "uid": user.uid,
                "username": user.username,
                "permission": user.permission,
                "dept_id": user.dept_id,
            },
            "items": models_to_payload(models),
            "image_items": [{"id": m.id, "name": m.alias or m.model_name, "display_name": m.display_name}
                for m in models if is_image_model(m)],
        }
    )
