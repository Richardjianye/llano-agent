"""MCP 平台、目录、授权与调用审计管理。"""

from fastapi import APIRouter, Depends
from model.request.mcp import (McpInstanceCreate, McpInstanceUpdate,
                               McpPermissionBatch)
from model.response import Response, UserInfo
from mcp_adapters import get_adapter, list_adapters
from services import mcp as service
from utils.dependencies import require_permission

router = APIRouter(prefix="/mcp", tags=["MCP 管理"])


def _instance_payload(row) -> dict:
    adapter = get_adapter(row.adapter_key)
    config = service._loads(row.config_json, {})
    configured = config.pop("_configured_secret_fields", [])
    return {
        "id": row.id, "code": row.code, "name": row.name,
        "adapter_key": row.adapter_key, "adapter_name": adapter.display_name,
        "config": config,
        "configured_secret_fields": configured, "status": row.status,
        "remark": row.remark,
    }


def _capability_payload(row) -> dict:
    return {
        "id": row.id, "code": row.capability_code, "name": row.name,
        "domain": row.domain, "description": row.description,
        "method": row.method, "path": row.path,
        "input_schema": service._loads(row.input_schema_json, {}),
        "metadata": service._loads(row.metadata_json, {}), "status": row.status,
    }


def _call_log_payload(row, user) -> dict:
    return {
        "id": row.id, "request_id": row.request_id,
        # UID 仍保留用于审计追踪；管理界面优先展示可识别的登录用户名。
        "user_uid": row.user_uid,
        "username": user.username if user else row.user_uid,
        "capability_code": row.capability_code, "status": row.status,
        "error_code": row.error_code, "latency_ms": row.latency_ms,
        "upstream_request_id": row.upstream_request_id,
        "response_size": row.response_size,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds")
        if row.created_at else None,
    }


@router.get("/adapters", response_model=Response)
async def adapters(_: UserInfo = Depends(require_permission("mcp:view"))):
    return Response(data={"items": list_adapters()})


@router.get("/instances", response_model=Response)
async def instances(_: UserInfo = Depends(require_permission("mcp:view"))):
    return Response(data={"items": [_instance_payload(row) for row in await service.list_instances()]})


@router.post("/instances", response_model=Response)
async def instance_create(body: McpInstanceCreate, _: UserInfo = Depends(require_permission("mcp:edit"))):
    instance_id = await service.create_instance(body.code, body.name, body.adapter_key,
                                                body.auth_config, body.config, body.remark)
    return Response(data={"id": instance_id})


@router.post("/instances/{instance_id}", response_model=Response)
async def instance_update(instance_id: int, body: McpInstanceUpdate,
                          _: UserInfo = Depends(require_permission("mcp:edit"))):
    values = body.model_dump(exclude_unset=True)
    if "auth_config" in values:
        values["auth"] = values.pop("auth_config")
    await service.update_instance(instance_id, **values)
    return Response()


@router.post("/instances/{instance_id}/sync", response_model=Response)
async def instance_sync(instance_id: int, _: UserInfo = Depends(require_permission("mcp:edit"))):
    return Response(data=await service.sync_capabilities(instance_id))


@router.get("/instances/{instance_id}/sync-preview", response_model=Response)
async def instance_sync_preview(instance_id: int,
                                _: UserInfo = Depends(require_permission("mcp:view"))):
    return Response(data=await service.preview_capability_sync(instance_id))


@router.post("/instances/{instance_id}/health", response_model=Response)
async def instance_health(instance_id: int,
                          _: UserInfo = Depends(require_permission("mcp:edit"))):
    return Response(data=await service.check_health(instance_id))


@router.post("/instances/{instance_id}/capabilities/{capability_id}/status", response_model=Response)
async def capability_status(instance_id: int, capability_id: int, status: int,
                            _: UserInfo = Depends(require_permission("mcp:edit"))):
    if status not in {0, 1}:
        from utils.errors import ValidationError_
        raise ValidationError_("status 只能为 0 或 1")
    await service.set_capability_status(instance_id, capability_id, status)
    return Response()


@router.get("/instances/{instance_id}/capabilities", response_model=Response)
async def capabilities(instance_id: int, query: str = None, domain: str = None,
                       _: UserInfo = Depends(require_permission("mcp:view"))):
    rows = await service.list_capabilities(instance_id, query=query, domain=domain)
    return Response(data={"items": [_capability_payload(row) for row in rows]})


@router.get("/instances/{instance_id}/permissions", response_model=Response)
async def permissions(instance_id: int, user_uid: str = None,
                      _: UserInfo = Depends(require_permission("mcp:view"))):
    rows = await service.list_permissions(instance_id, user_uid)
    return Response(data={"items": [{
        "id": permission.id, "user_uid": permission.user_uid,
        "username": user.realname or user.username, "capability_code": capability.capability_code,
        "capability_name": capability.name, "domain": capability.domain,
        "effect": permission.effect,
        "constraints": service._loads(permission.constraints_json, {}),
        "remark": permission.remark,
        "expires_at": permission.expires_at.isoformat() if permission.expires_at else None,
        "status": permission.status,
    } for permission, capability, user in rows]})


@router.post("/instances/{instance_id}/permissions", response_model=Response)
async def permission_replace(instance_id: int, body: McpPermissionBatch,
                             operator: UserInfo = Depends(require_permission("mcp:edit"))):
    if not body.replace:
        # 首版保持语义单一，避免增量授权与撤销产生残留权限。
        from utils.errors import ValidationError_
        raise ValidationError_("当前接口仅支持 replace=true 的全量替换")
    count = await service.replace_permissions(body.user_uid, instance_id,
        body.capability_codes, body.effect, body.constraints, body.expires_at, operator.uid,
        body.remark)
    return Response(data={"count": count})


@router.get("/instances/{instance_id}/logs", response_model=Response)
async def logs(instance_id: int, limit: int = 100,
               _: UserInfo = Depends(require_permission("mcp:view"))):
    rows = await service.list_call_logs(instance_id, max(1, min(limit, 500)))
    return Response(data={"items": [_call_log_payload(row, user) for row, user in rows]})
