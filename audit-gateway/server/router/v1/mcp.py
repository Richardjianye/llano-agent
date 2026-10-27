"""用户可调用的 MCP 搜索与执行入口；API Key 只继承所属用户授权。

CUSTOM（决策 #45 / v2 专章 P0-1）：两个端点都先过**会话作用域**闸门。
群聊里的 agent 不得使用任何 MCP —— 能力不跨界，只有产物跨界。
判定与理由全在 services/conversation_scope.py，这里只负责接上去。
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from mcp_adapters import get_adapter
from model.request.mcp import McpExecuteRequest, McpSearchRequest
from services import conversation_scope as scope_service
from services import mcp as service
from utils.errors import AppError, McpUpstreamError
from utils.gateway_auth import Caller, require_caller
from utils.middleware import get_request_id

router = APIRouter(prefix="/mcp")


def _access_message(status: str) -> str:
    return {
        "available": "当前可调用",
        "request_access": "接口存在；当前未授权，请联系负责人开通",
        "expired": "授权已过期，请联系负责人续期",
        "setup_required": "请先绑定并测试自己的中台账号",
        "unavailable": "接口当前已停用或目录待管理员复核",
    }[status]


def _capability_payload(entry: service.CatalogEntry) -> dict:
    instance = entry.instance
    capability = entry.capability
    status = entry.access_status
    payload = {
        "instance": instance.code, "code": capability.capability_code,
        "name": capability.name, "domain": capability.domain,
        "description": capability.description, "access_status": status,
        "access_message": _access_message(status),
    }
    # 未授权目录只负责告知“能力存在”，避免泄露调用细节或诱导模型直接构造参数。
    if status == "available":
        payload["input_schema"] = service._loads(capability.input_schema_json, {})
    return payload


def _category_payloads(rows, limit: int) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for entry in rows:
        instance = entry.instance
        capability = entry.capability
        key = (instance.code, capability.domain)
        item = grouped.setdefault(key, {
            "instance": instance.code, "domain": capability.domain,
            "total": 0, "available": 0, "request_access": 0,
            "expired": 0, "setup_required": 0, "unavailable": 0,
            "drilldown": {
                "instance_code": instance.code,
                "domain": capability.domain,
            },
        })
        status = entry.access_status
        item["total"] += 1
        item[status] += 1
    return list(grouped.values())[:limit]


@router.post("/capabilities/search")
async def search(body: McpSearchRequest, request: Request,
                 caller: Caller = Depends(require_caller)):
    # 群聊里连**目录**都不能给。
    #
    # 只挡 execute 不挡 search 看上去够了（反正执行会被拒），但那等于对外公布
    # 我们有哪些接口 —— 而 P0-1 的第四条约束正是「错误文案不得泄漏存在性」。
    # 一个能读到完整能力目录的人，已经拿到了他不该有的东西。
    #
    # 返回空目录而不是报错：agent 会把「一个能力都没有」如实转述成「我这儿查不到
    # 这类接口」，这正是软失败要的那种表现。
    decision = scope_service.resolve_scope(caller, request.headers.get(scope_service.SCOPE_HEADER))
    if not decision.allows_capabilities:
        return {
            "object": "list", "level": "capabilities",
            "hint": scope_service.SCOPE_DENIED_MESSAGE,
            "data": [],
        }

    rows = await service.search_catalog(
        caller.uid, body.query, body.instance_code, body.domain, body.limit)
    if not body.query and not body.domain:
        return {
            "object": "list", "level": "categories",
            "hint": "选择 domain 下钻查看接口；available 可直接调用，其余状态需按提示处理",
            "data": _category_payloads(rows, body.limit),
        }
    return {
        "object": "list", "level": "capabilities",
        "hint": "只有 available 能力可执行；setup_required 需先绑定账号，其他状态按提示处理",
        "data": [_capability_payload(entry) for entry in rows],
    }


@router.post("/execute")
async def execute(body: McpExecuteRequest, request: Request,
                  caller: Caller = Depends(require_caller)):
    started = time.perf_counter()
    request_id = get_request_id(request)
    instance = db_cap = None

    # 作用域闸门放在**最前面**，在解析任何参数、碰任何数据库之前。
    #
    # 「必定失败」不真打上游（P0-1 第二条约束）：本地判定就回同形态错误。真发请求
    # 会让中台看到大量无效 Token，可能撞上已登记的「共享网关 IP 被当撞库拦截」风险
    # —— 为演一场戏去惹风控不划算。
    decision = scope_service.resolve_scope(caller, request.headers.get(scope_service.SCOPE_HEADER))
    if not decision.allows_capabilities:
        # 单独计数、单独限流。撞太多次之后不再落库（计数照涨），
        # 否则 @ 轰炸就是免费刷我们的库。
        if await scope_service.note_scope_denial(caller.uid):
            await service.record_call(
                request_id=request_id, uid=caller.uid, api_key_id=caller.api_key_id,
                code=body.capability_code, arguments=body.arguments,
                status=scope_service.SCOPE_DENIED, error_code=scope_service.SCOPE_DENIED_CODE,
                latency_ms=int((time.perf_counter() - started) * 1000))
        # 403 与普通的未授权同形态：对提问者表现为「网关不给我干」，
        # 而不是「这个 agent 有个特殊模式」。
        return JSONResponse(status_code=403, content=scope_service.denial_payload())

    try:
        resolved = await service.resolve_capability(
            caller.uid, body.capability_code, body.instance_code, request_id)
        instance = resolved.instance
        db_cap = resolved.db_capability
        service.validate_arguments(resolved.trusted, body.arguments)
        service.enforce_constraints(body.arguments, resolved.constraints)
        result = await get_adapter(instance.adapter_key).execute(
            resolved.trusted, body.arguments, resolved.auth,
            service._loads(instance.config_json, {}))
        encoded = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) > 10 * 1024 * 1024:
            raise RuntimeError("MCP 上游响应超过 10MB 安全上限")
        upstream_request_id = None
        if isinstance(result, dict):
            upstream_request_id = result.get("request_id") or result.get("trace_id")
        await service.record_call(request_id=request_id, uid=caller.uid, api_key_id=caller.api_key_id,
            code=body.capability_code, arguments=body.arguments, status="success", error_code=None,
            latency_ms=int((time.perf_counter() - started) * 1000), response_size=len(encoded),
            instance_id=instance.id, capability_id=db_cap.id,
            upstream_request_id=str(upstream_request_id)[:128] if upstream_request_id else None)
        return {"request_id": request_id, "capability_code": body.capability_code, "result": result}
    except McpUpstreamError as exc:
        await service.record_call(request_id=request_id, uid=caller.uid, api_key_id=caller.api_key_id,
            code=body.capability_code, arguments=body.arguments, status="failed",
            error_code=exc.error_code, latency_ms=int((time.perf_counter() - started) * 1000),
            instance_id=instance.id if instance else None, capability_id=db_cap.id if db_cap else None)
        return JSONResponse(status_code=exc.http_status, content={"error": {
            "message": exc.message, "type": "mcp_upstream_error", "code": exc.error_code}})
    except AppError as exc:
        await service.record_call(request_id=request_id, uid=caller.uid, api_key_id=caller.api_key_id,
            code=body.capability_code, arguments=body.arguments, status="denied",
            error_code=str(exc.code), latency_ms=int((time.perf_counter() - started) * 1000),
            instance_id=instance.id if instance else None, capability_id=db_cap.id if db_cap else None)
        return JSONResponse(status_code=403, content={"error": {
            "message": exc.message, "type": "mcp_permission_error", "code": "mcp_forbidden"}})
    except Exception:
        await service.record_call(request_id=request_id, uid=caller.uid, api_key_id=caller.api_key_id,
            code=body.capability_code, arguments=body.arguments, status="failed",
            error_code="upstream_error", latency_ms=int((time.perf_counter() - started) * 1000),
            instance_id=instance.id if instance else None, capability_id=db_cap.id if db_cap else None)
        return JSONResponse(status_code=502, content={"error": {
            "message": "MCP 上游调用失败", "type": "mcp_upstream_error", "code": "mcp_upstream_error"}})
