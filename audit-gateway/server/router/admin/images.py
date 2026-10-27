"""生图管理：发现不收费，管理员试生成走共用审计和配额。"""
import json
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from db.selects.catalog import get_model, get_provider
from model.response import Response
from services.image_catalog import discover
from services.image_generation import ImageRequest, decode_image, is_image_model
from services.forwarding import UpstreamError
from router.v1.images import execute_image_request
from utils.dependencies import require_permission
from utils.errors import NotFoundError, ValidationError_
from utils.gateway_auth import Caller

router = APIRouter(prefix="/images", tags=["生图渠道管理"])

@router.post("/provider/{provider_id}/probe", response_model=Response)
async def probe(provider_id: int, _=Depends(require_permission("model:edit"))):
    provider = await get_provider(provider_id)
    if not provider or provider.base_url.lower().startswith("codex://"):
        raise NotFoundError("生图供应商不存在")
    try:
        models = await discover(provider)
    except UpstreamError as exc:
        raise ValidationError_(str(exc)) from exc
    except Exception as exc:
        raise ValidationError_("渠道凭据不可用，请重新配置 Key") from exc
    return Response(data={"provider_id": provider_id, "models": models})

class ImageTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: int = Field(..., gt=0)
    prompt: str = Field(..., min_length=1, max_length=16000)
    size: str = "1024x1024"
    reference_images: list[str] = Field(default_factory=list, max_length=3)

@router.post("/test", response_model=Response)
async def test_image(request: Request, body: ImageTest, operator=Depends(require_permission("model:edit"))):
    model = await get_model(body.model_id)
    if not model or model.status != 1 or not is_image_model(model):
        raise ValidationError_("请选择已启用且支持的生图模型")
    try:
        image = ImageRequest(prompt=body.prompt, size=body.size, reference_images=body.reference_images,
            session_id="admin-image-test")
        if not image.prompt.strip():
            raise ValueError("提示词不能为空")
        for ref in image.reference_images:
            decode_image(ref, data_url=True)
    except ValueError as exc:
        raise ValidationError_("请检查提示词、图片比例及内联参考图") from exc
    caller = Caller(uid=operator.uid, username=operator.username, role=operator.permission,
        dept_id=operator.dept_id, dept_code=operator.dept_code)
    result = await execute_image_request(request, caller, image, model)
    if not isinstance(result, dict):
        error = json.loads(result.body).get("error", {})
        raise ValidationError_(error.get("message") or "试生成失败")
    return Response(data=result)
