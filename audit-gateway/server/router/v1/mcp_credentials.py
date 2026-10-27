"""用户自己的 MCP 上游凭据绑定、测试与清除接口。"""

from fastapi import APIRouter, Depends, Request

from model.request.mcp_credential import McpCredentialBind
from model.response import Response
from services import mcp_credentials as service
from utils.gateway_auth import Caller, require_caller
from utils.middleware import get_client_ip, get_request_id

router = APIRouter(prefix="/mcp/instances/{instance_code}/credential")


@router.get("", response_model=Response)
async def credential_status(
    instance_code: str,
    caller: Caller = Depends(require_caller),
):
    return Response(data=await service.credential_status(caller.uid, instance_code))


@router.put("", response_model=Response)
async def credential_bind(
    instance_code: str,
    body: McpCredentialBind,
    request: Request,
    caller: Caller = Depends(require_caller),
):
    data = await service.bind_and_verify(
        caller.uid,
        instance_code,
        body.account,
        body.password,
        get_request_id(request),
        get_client_ip(request),
    )
    return Response(data=data)


@router.post("/test", response_model=Response)
async def credential_test(
    instance_code: str,
    request: Request,
    caller: Caller = Depends(require_caller),
):
    data = await service.verify_saved(
        caller.uid,
        instance_code,
        get_request_id(request),
        get_client_ip(request),
    )
    return Response(data=data)


@router.delete("", response_model=Response)
async def credential_delete(
    instance_code: str,
    request: Request,
    caller: Caller = Depends(require_caller),
):
    await service.delete_credential(
        caller.uid,
        instance_code,
        get_request_id(request),
        get_client_ip(request),
    )
    return Response()
