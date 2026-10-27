"""Codex 上游渠道管理（仅 admin）

一条 Codex 渠道 = 一个 ChatGPT/Codex 账号的 OAuth 凭证 + 一个 OpenAI 兼容
对外口径。员工侧请求照常打 /v1/chat/completions，转发层识别 codex:// 哨兵
地址后走 Codex 适配层（登录/凭证/连通核心在 services/codex_channel/）。

凭证（refresh_token / access_token）与上游 API Key 同一条 Fernet 加密通道，
只进不出：本模块任何响应都不回带 token 明文。
"""

from typing import Optional

from db.inserts.catalog import insert_provider
from db.selects.catalog import get_provider, get_provider_by_code
from db.updates.catalog import update_provider
from fastapi import APIRouter, Depends
from loguru import logger
from model.request.codex import (CodexOAuthExchange, CodexOAuthStart,
                                 CodexProviderImport)
from model.response import Response
from services.codex_channel import (CodexCredential, CodexUpstreamError,
                                    OAuthError, ensure_access_token,
                                    load_credential,
                                    probe_models, refresh_tokens,
                                    save_credential)
from utils.dependencies import require_permission
from utils.errors import NotFoundError, ValidationError_
from utils.secret_box import SecretKeyMissing

router = APIRouter(prefix="/codex", tags=["Codex 渠道"])

CODEX_BASE_DEFAULT = "codex://chatgpt"


def _normalize_base(base_url: Optional[str]) -> str:
    base = (base_url or "").strip() or CODEX_BASE_DEFAULT
    if not base.lower().startswith("codex://"):
        raise ValidationError_("base_url 需以 codex:// 开头（如 codex://chatgpt）")
    from services.codex_channel.upstream import resolve_base_url
    try:
        resolve_base_url(base)
    except CodexUpstreamError as exc:
        raise ValidationError_(str(exc)) from exc
    return base.rstrip("/")


async def _upsert_provider(
    provider_code: str, name: str, base_url: str, credential: CodexCredential
) -> tuple:
    """按 provider_code 建/更渠道行。返回 (provider_id, 是否更新)"""
    try:
        existing = await get_provider_by_code(provider_code)
        if existing is not None:
            if not existing.base_url.lower().startswith("codex://"):
                raise ValidationError_("该渠道编码已被普通上游使用，请使用新的 Codex 渠道编码")
            await update_provider(existing.id, name=name, base_url=base_url, api_key=credential.to_json())
            return existing.id, True
        provider_id = await insert_provider(
            code=provider_code,
            name=name,
            base_url=base_url,
            api_key=credential.to_json(),
            timeout_seconds=120,
            remark="Codex 渠道",
        )
        return provider_id, False
    except SecretKeyMissing as exc:
        raise ValidationError_(str(exc)) from exc


@router.post("/oauth/start", response_model=Response)
async def oauth_start(
    body: CodexOAuthStart,
    operator=Depends(require_permission("model:edit")),
):
    """第一步：生成授权 URL，由**人工**在浏览器完成授权

    AI/服务端不接触账号密码、扫码与验证码；授权完成后把回调 URL 粘到
    oauth/exchange 即可。会话有效期 30 分钟。
    """
    from services.codex_channel.oauth import start_gateway_login
    try:
        session_id, auth_url = await start_gateway_login(operator.uid, body.redirect_uri)
    except OAuthError as exc:
        raise ValidationError_(str(exc)) from exc
    return Response(data={"session_id": session_id, "auth_url": auth_url})


@router.post("/oauth/exchange", response_model=Response)
async def oauth_exchange(
    body: CodexOAuthExchange,
    operator=Depends(require_permission("model:edit")),
):
    """第二步：粘贴含 state 的回调 URL，兑换凭证并落库为 Codex 渠道"""
    base = _normalize_base(body.base_url)
    existing = await get_provider_by_code(body.provider_code)
    if existing is not None and not existing.base_url.lower().startswith("codex://"):
        raise ValidationError_("该渠道编码已被普通上游使用，请使用新的 Codex 渠道编码")
    try:
        from services.codex_channel.oauth import exchange_gateway_code
        tokens, identity = await exchange_gateway_code(operator.uid, body.session_id, body.code)
    except OAuthError as exc:
        raise ValidationError_(str(exc)) from exc

    cred = CodexCredential(tokens=tokens, identity=identity)
    name = (body.name or "").strip() or identity.email or provider_default_name(body.provider_code)
    provider_id, updated = await _upsert_provider(
        body.provider_code, name, base, cred
    )
    logger.info(
        f"Codex 渠道{'更新' if updated else '创建'}: id={provider_id} "
        f"code={body.provider_code} email={identity.email} by={operator.uid}"
    )
    return Response(data={
        "provider_id": provider_id,
        "updated": updated,
        "email": identity.email,
        "plan_type": identity.plan_type,
        "chatgpt_account_id": identity.chatgpt_account_id,
    })


@router.post("/provider/import", response_model=Response)
async def provider_import(
    body: CodexProviderImport,
    operator=Depends(require_permission("model:edit")),
):
    """直接导入 refresh_token（不走浏览器授权）

    先用 refresh_token 换一次 access_token：拿到账号身份，同时验证 token 有效。
    """
    base = _normalize_base(body.base_url)
    existing = await get_provider_by_code(body.provider_code)
    if existing is not None and not existing.base_url.lower().startswith("codex://"):
        raise ValidationError_("该渠道编码已被普通上游使用，请使用新的 Codex 渠道编码")
    try:
        tokens, identity = await refresh_tokens(body.refresh_token)
    except OAuthError as exc:
        raise ValidationError_(str(exc)) from exc

    cred = CodexCredential(tokens=tokens, identity=identity)
    name = (body.name or "").strip() or identity.email or provider_default_name(body.provider_code)
    provider_id, updated = await _upsert_provider(
        body.provider_code, name, base, cred
    )
    logger.info(
        f"Codex 渠道导入 refresh_token: id={provider_id} code={body.provider_code} "
        f"email={identity.email} by={operator.uid}"
    )
    return Response(data={
        "provider_id": provider_id,
        "updated": updated,
        "email": identity.email,
        "plan_type": identity.plan_type,
        "chatgpt_account_id": identity.chatgpt_account_id,
    })


def provider_default_name(provider_code: str) -> str:
    return f"Codex-{provider_code}"


async def _load(provider_id: int):
    provider = await get_provider(provider_id)
    if provider is None or not provider.base_url.lower().startswith("codex://"):
        raise NotFoundError(f"Codex 渠道 {provider_id} 不存在")
    return provider


@router.get("/provider/{provider_id}/usage", response_model=Response)
async def provider_usage(
    provider_id: int,
    _=Depends(require_permission("model:view")),
):
    """剩余额度：5h 主窗口 + 7d 副窗口用量与重置时间，外加重置券

    数据源是 ChatGPT 后端 wham/usage，查的是账号此刻的真实额度。
    """
    from services.codex_channel import CodexUpstreamError, ensure_access_token, fetch_usage

    provider = await _load(provider_id)
    try:
        _, cred = await ensure_access_token(provider.id, provider.api_key_encrypted)
    except Exception as exc:
        raise ValidationError_(f"凭证不可用：{exc}") from exc

    try:
        usage = await fetch_usage(cred, provider.base_url, provider.timeout_seconds or 30)
    except CodexUpstreamError as exc:
        raise ValidationError_(str(exc)) from exc

    return Response(data={"provider_id": provider_id, "usage": usage})


@router.get("/provider/{provider_id}/status", response_model=Response)
async def provider_status(
    provider_id: int,
    _=Depends(require_permission("model:view")),
):
    """渠道状态：账号身份 + 凭证有效期（不回带任何 token）"""
    provider = await _load(provider_id)
    try:
        cred = load_credential(provider.api_key_encrypted)
    except Exception as exc:
        return Response(data={
            "provider_id": provider_id,
            "code": provider.code,
            "name": provider.name,
            "base_url": provider.base_url,
            "status": provider.status,
            "credential_ok": False,
            "credential_error": str(exc),
        })
    from services.codex_channel.health import snapshot as health_snapshot

    return Response(data={
        "provider_id": provider_id,
        "code": provider.code,
        "name": provider.name,
        "base_url": provider.base_url,
        "status": provider.status,
        "credential_ok": True,
        "email": cred.identity.email,
        "plan_type": cred.identity.plan_type,
        "chatgpt_account_id": cred.identity.chatgpt_account_id,
        "access_expires_at": cred.tokens.expires_at or None,
        "access_fresh": cred.access_fresh(),
        # 掉号检测快照（被动标记 + 周期巡检的结果），未知时为 None
        "health": health_snapshot(provider_id),
    })


@router.post("/provider/{provider_id}/probe", response_model=Response)
async def provider_probe(
    provider_id: int,
    _=Depends(require_permission("model:edit")),
):
    """连通探测：续期凭证（如需）→ 拉账号真实模型清单

    返回的 models 用来在「模型管理」里绑定 model_name；
    raw 是上游清单原文（schema 会变，排查问题时看它）。
    """
    provider = await _load(provider_id)
    try:
        _, cred = await ensure_access_token(provider.id, provider.api_key_encrypted)
    except Exception as exc:
        raise ValidationError_(f"凭证不可用：{exc}") from exc

    try:
        manifest = await probe_models(cred, provider.base_url, provider.timeout_seconds or 15)
    except CodexUpstreamError as exc:
        raise ValidationError_(str(exc)) from exc

    return Response(data={
        "provider_id": provider_id,
        "ok": True,
        "models": manifest.get("models") or [],
        "raw": manifest.get("raw"),
    })


@router.get("/providers", response_model=Response)
async def providers(operator=Depends(require_permission("model:view"))):
    """只读配置与凭据/健康快照；加载页面不触发授权或上游探测。"""
    from db.selects.catalog import list_providers, list_models
    from collections import Counter
    counts = Counter(m.provider_id for m, _ in await list_models())
    items = []
    for provider in await list_providers():
        if provider.base_url.lower().startswith("codex://"):
            info = await provider_status(provider.id, operator)
            items.append({**info.data, "id": provider.id, "kind": "codex", "api_key_masked": "已配置" if provider.api_key_encrypted else "未配置", "timeout_seconds": provider.timeout_seconds,
                "remark": provider.remark, "model_count": counts[provider.id]})
    return Response(data={"items": items})
