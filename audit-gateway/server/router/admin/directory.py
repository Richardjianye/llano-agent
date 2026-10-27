"""只有 user:edit 管理员可以读取企业名册和提交人工确认。"""
from fastapi import APIRouter, Depends, Response as HttpResponse
from model.request.directory import DirectoryApply, DirectoryExport
from model.response import Response, UserInfo
from services import directory_sync
from utils.dependencies import require_permission

router = APIRouter(prefix="/account/directory", tags=["通讯录同步"])


@router.post("/preview", response_model=Response)
async def preview(response: HttpResponse, operator: UserInfo = Depends(require_permission("user:edit"))):
    response.headers["Cache-Control"] = "no-store"
    return Response(data=await directory_sync.preview(operator))


@router.post("/apply", response_model=Response)
async def apply(body: DirectoryApply, response: HttpResponse,
                operator: UserInfo = Depends(require_permission("user:edit"))):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return Response(data=await directory_sync.apply(body, operator))


@router.post("/export-audit", response_model=Response)
async def export_audit(body: DirectoryExport, response: HttpResponse,
                       operator: UserInfo = Depends(require_permission("user:edit"))):
    response.headers["Cache-Control"] = "no-store"
    await directory_sync.audit_export(body, operator)
    return Response()
