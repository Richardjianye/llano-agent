"""生图也经过身份、模型授权、配额、脱敏与用量审计。"""
from __future__ import annotations

import asyncio
import re
import time

from db.selects.apikey import api_key_used_tokens
from db.selects.catalog import get_provider
from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from router.v1.chat import _openai_error, _record_in_background
from services import conversation_scope
from services.forwarding import UpstreamError, build_target
from services.image_generation import ImageRequest, decode_image, generate, is_image_model
from services.redaction_policy import keywords_for
from services.model_access import check_model_permission, list_available_models, model_public_name
from services.quota import applicable_quota_statuses, first_blocking_quota
from services.usage import UsageFacts, estimate_tokens, extract_breakdown, extract_usage
from services.usage_query import ModelPrice
from utils.gateway_auth import Caller, require_caller
from utils.middleware import get_request_id

router = APIRouter(prefix="/images")


@router.get("/models")
async def image_models(request: Request, caller: Caller = Depends(require_caller)):
    if not conversation_scope.resolve_scope(caller, request.headers.get(conversation_scope.SCOPE_HEADER)).allows_capabilities:
        return {"object": "list", "data": []}
    models = [m for m in await list_available_models(caller.as_subject()) if is_image_model(m)]
    return {"object": "list", "data": [
        {"id": model_public_name(m), "display_name": m.display_name} for m in models]}


@router.post("/generations")
async def image_generate(request: Request, caller: Caller = Depends(require_caller)):
    if not conversation_scope.resolve_scope(caller, request.headers.get(conversation_scope.SCOPE_HEADER)).allows_capabilities:
        return _openai_error(conversation_scope.SCOPE_DENIED_MESSAGE, 403, "scope_denied")
    try:
        body = ImageRequest.model_validate(await request.json())
        if not body.prompt.strip():
            return _openai_error("生图提示词不能为空")
        for ref in body.reference_images:
            decode_image(ref, data_url=True)
    except (ValueError, ValidationError):
        return _openai_error("生图参数无效；请检查提示词、尺寸和内联参考图")
    if not body.model:
        models = [m for m in await list_available_models(caller.as_subject()) if is_image_model(m)]
        models.sort(key=lambda m: (m.model_name != "gpt-image-2", model_public_name(m)))
        body.model = model_public_name(models[0]) if models else ""
    model = await check_model_permission(caller.as_subject(), body.model) if body.model else None
    if model is None or not is_image_model(model):
        return _openai_error("当前账号尚未开通该生图模型，请联系管理员", 403, "model_not_allowed")
    return await execute_image_request(request, caller, body, model)


async def execute_image_request(request: Request, caller: Caller, body: ImageRequest, model):
    """调用与管理员显式试生成共用；调用方负责验证模型访问权限。"""
    if caller.api_key_id is not None and caller.api_key_token_limit is not None:
        if await api_key_used_tokens(caller.api_key_id) >= caller.api_key_token_limit:
            return _openai_error("当前 API Key 额度已用完", 429, "api_key_quota_exceeded")
    if first_blocking_quota(await applicable_quota_statuses(caller.uid, caller.dept_id, model.id)):
        return _openai_error("该生图模型今日额度已用完", 429, "quota_exceeded")
    provider = await get_provider(model.provider_id)
    if provider is None:
        return _openai_error("生图模型对应渠道不可用", 503, "provider_missing")
    try:
        keywords = await keywords_for(caller.dept_id, strict=True)
    except Exception:
        return _openai_error("发送策略暂不可读取，图片请求尚未发送，请稍后重试", 503, "redaction_unavailable")
    from config import get_settings
    original_prompt = body.prompt
    if keywords:
        pattern = re.compile("|".join(re.escape(word) for word in keywords), re.IGNORECASE)
        body.prompt = pattern.sub(lambda _: get_settings().mask_placeholder or "[公司]", body.prompt)
    masked = body.prompt != original_prompt
    started = time.perf_counter()
    facts = UsageFacts(request_id=get_request_id(request), user_uid=caller.uid,
        api_key_id=caller.api_key_id, dept_id=caller.dept_id, role=caller.role,
        provider_id=provider.id, model_id=model.id, model_name=model.model_name,
        call_kind="image", turn_kind="image", price=ModelPrice.from_model(model),
        request_messages=[{"role": "user", "content": [{"type": "text", "text": original_prompt}] + [
            {"type": "image_url", "image_url": {"url": ref}} for ref in body.reference_images]}],
        session_id=body.session_id or None, masked=masked,
        status="failed", tokens_estimated=True, prompt_tokens=estimate_tokens(body.prompt))
    facts.total_tokens = facts.prompt_tokens
    try:
        result = await generate(build_target(provider, model), body)
        usage_source = result
        raw_usage = result.get("usage")
        if isinstance(raw_usage, dict) and ("input_tokens" in raw_usage or "output_tokens" in raw_usage) and "prompt_tokens" not in raw_usage:
            usage_source = {"usage": {"prompt_tokens": raw_usage.get("input_tokens", 0),
                "completion_tokens": raw_usage.get("output_tokens", 0), "total_tokens": raw_usage.get("total_tokens", 0)}}
        usage = extract_usage(usage_source)
        breakdown = extract_breakdown(result)
        if usage:
            facts.prompt_tokens = usage["prompt_tokens"]
            facts.completion_tokens = usage["completion_tokens"]
            facts.total_tokens = usage["total_tokens"]
            facts.tokens_estimated = False
        facts.usage_raw = breakdown.raw
        facts.cached_prompt_tokens = breakdown.cached_prompt_tokens
        facts.cache_write_tokens = breakdown.cache_write_tokens
        facts.reasoning_tokens = breakdown.reasoning_tokens
        facts.status = "success"
        facts.response_text = "已生成 1 张图片（详见审计附件）"
        generated = result["data"][0]
        facts.request_messages.append({"role": "assistant", "content": [{"type": "image_url", "image_url": {
            "url": f"data:{generated['mime_type']};base64,{generated['b64_json']}"}}]})
        return {**result, "model": model_public_name(model)}
    except UpstreamError as exc:
        facts.error_code = exc.code
        raw = getattr(exc, "usage_raw", None)
        if isinstance(raw, dict):
            facts.usage_raw = raw
            usage = extract_usage({"usage": raw}) if "prompt_tokens" in raw else extract_usage({"usage": {
                "prompt_tokens": raw.get("input_tokens", 0), "completion_tokens": raw.get("output_tokens", 0),
                "total_tokens": raw.get("total_tokens", 0)}})
            if usage:
                facts.prompt_tokens = usage["prompt_tokens"]
                facts.completion_tokens = usage["completion_tokens"]
                facts.total_tokens = usage["total_tokens"]
                facts.tokens_estimated = False
        return _openai_error(str(exc), exc.status_code, exc.code)
    except asyncio.CancelledError:
        facts.aborted = True
        facts.error_code = "image_cancelled"
        raise
    finally:
        facts.latency_ms = int((time.perf_counter() - started) * 1000)
        _record_in_background(facts)
