"""Codex 后端连通（提取自 codex2api proxy/executor.go 的出站口径）

对 ``https://chatgpt.com/backend-api/codex/responses`` 发推理请求，
按真实 Codex CLI 的形态出站（UA / Originator / Version / Beta-Features /
Chatgpt-Account-Id），SSE 事件经 translate.StreamTranslator 翻回
chat.completion.chunk 后交给网关透传。

出站指纹常量与 codex2api 保持一致：UA 前缀与 Originator 恒为同一标识
（codex-tui），版本跟随上游已验证的 0.162.0-alpha.17.2。别单独改其中一项——两个
标识不一致是真实客户端不会有的破绽。
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

import httpx

from services.codex_channel.credentials import CodexCredential
from services.codex_channel.translate import StreamTranslator, build_responses_request

# ==================== 出站身份（Codex CLI 0.162.0-alpha.17.2） ====================

CODEX_CLIENT_NAME = "codex-tui"
# /models 按客户端版本下发目录；旧版本会漏掉账号已可用的新模型。
# Version、User-Agent 和探测 query 必须共同使用这个已实测的版本。
CODEX_CLIENT_VERSION = "0.162.0-alpha.17.2"
CODEX_USER_AGENT = (
    f"{CODEX_CLIENT_NAME}/{CODEX_CLIENT_VERSION} "
    "(Mac OS 15.5.0; arm64) xterm-256color "
    f"({CODEX_CLIENT_NAME}; {CODEX_CLIENT_VERSION})"
)
CODEX_BETA_FEATURES = "remote_compaction_v2"
OFFICIAL_BACKEND_BASE = "https://chatgpt.com/backend-api/codex"


class CodexUpstreamError(RuntimeError):
    """上游不可用或返回错误（转发层包成 UpstreamError）"""

    def __init__(self, message: str, status_code: int = 502, code: str = "codex_upstream_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def resolve_base_url(provider_base_url: str) -> str:
    """把渠道登记的 ``codex://...`` 哨兵解析成真实后端地址

    - ``codex://chatgpt`` → 官方后端 https://chatgpt.com/backend-api/codex
    - ``codex://host[:port]`` → https://host[:port]/backend-api/codex（同协议自建中转）
    - ``codex://host[:port]/path`` → https://host[:port]/path（中转已带路径时原样使用）
    """
    from urllib.parse import urlsplit
    raw = (provider_base_url or "").strip()
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme.lower() == "codex" and bool(parsed.hostname)
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise CodexUpstreamError("Codex 渠道地址无效", 500, "bad_codex_base_url")
    if parsed.hostname.lower() == "chatgpt" and not parsed.path.strip("/") and parsed.port is None:
        return OFFICIAL_BACKEND_BASE
    path = parsed.path.rstrip("/") or "/backend-api/codex"
    return "https://" + parsed.netloc + path


def _safe_detail(text: str, credential: CodexCredential) -> str:
    for token in (credential.tokens.access_token, credential.tokens.refresh_token, credential.tokens.id_token):
        if token:
            text = text.replace(token, "[redacted]")
    return text[:500]


def build_headers(access_token: str, account_id: str = "", accept: str = "text/event-stream") -> Dict[str, str]:
    """出站请求头（伪装形态与 codex2api applyCodexRequestHeaders 一致）"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": CODEX_USER_AGENT,
        "Originator": CODEX_CLIENT_NAME,
        "Version": CODEX_CLIENT_VERSION,
        "X-Codex-Beta-Features": CODEX_BETA_FEATURES,
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    return headers


def apply_session_headers(headers: Dict[str, str], cache_key: str) -> None:
    """会话标识头。真实客户端每个请求都带三件套；没有会话键时整体跳过
    （与 codex2api ApplyCodexSessionHeaders 对空会话键的行为一致）"""
    if not cache_key:
        return
    headers["Session-Id"] = cache_key
    headers["Thread-Id"] = cache_key
    headers["x-client-request-id"] = cache_key


# ==================== SSE 解析 ====================


async def parse_sse_events(lines: AsyncGenerator[str, None]) -> AsyncGenerator[Dict[str, Any], None]:
    """把上游 SSE 行流解析成事件字典（以 data 里的 type 为准）"""
    data_buf: list = []

    async def flush():
        if not data_buf:
            return None
        raw = "\n".join(data_buf).strip()
        data_buf.clear()
        if not raw or raw == "[DONE]":
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async for line in lines:
        if line.startswith("data:"):
            data_buf.append(line[5:].strip())
            continue
        if not line.strip():
            event = await flush()
            if event is not None:
                yield event
    event = await flush()
    if event is not None:
        yield event


# ==================== 推理调用 ====================


async def stream_chat(
    credential: CodexCredential,
    provider_base_url: str,
    payload: Dict[str, Any],
    cache_key: str = "",
    timeout_seconds: int = 60,
) -> AsyncGenerator[Tuple[str, Optional[Dict[str, Any]]], None]:
    """流式推理。yield (SSE 行, 解析出的 chat chunk|None)，与 forwarding.stream_upstream 同契约

    上游固定按流式取（Codex 对 stream:false 的支持不稳），要非流式的调用方
    自己用 StreamTranslator.final_response() 装配。
    """
    base = resolve_base_url(provider_base_url)
    body = build_responses_request(payload, cache_key=cache_key)
    headers = build_headers(
        credential.tokens.access_token, credential.identity.chatgpt_account_id
    )
    apply_session_headers(headers, cache_key)

    translator = StreamTranslator(str(body.get("model") or ""))
    url = f"{base}/responses"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    detail = _safe_detail((await resp.aread()).decode("utf-8", "ignore"), credential)
                    if resp.status_code == 401:
                        raise CodexUpstreamError(
                            f"Codex 凭证被上游拒绝（401），请重新登录。详情：{detail}",
                            401, "codex_auth_failed",
                        )
                    raise CodexUpstreamError(
                        f"上游返回 {resp.status_code}: {detail}",
                        resp.status_code, f"upstream_{resp.status_code}",
                    )

                async for event in parse_sse_events(resp.aiter_lines()):
                    chunk, terminal, failed = translator.translate(event)
                    if failed:
                        detail = (chunk or {}).get("error", {}).get("message", "Codex 上游失败")
                        raise CodexUpstreamError(_safe_detail(str(detail), credential), 502, "codex_response_failed")
                    if chunk is not None:
                        line = f"data: {json.dumps(chunk, ensure_ascii=False)}"
                        yield line, (chunk if "error" not in chunk else None)
                    if terminal:
                        return
                raise CodexUpstreamError("Codex 响应在完成事件前中断", 502, "codex_incomplete_stream")
    except httpx.TimeoutException as exc:
        raise CodexUpstreamError(f"上游超时（{timeout_seconds}s）", 504, "timeout") from exc
    except httpx.HTTPError as exc:
        raise CodexUpstreamError(f"上游连接失败：{exc}", 502, "connect_failed") from exc


async def complete_chat(
    credential: CodexCredential,
    provider_base_url: str,
    payload: Dict[str, Any],
    cache_key: str = "",
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    """非流式推理：上游仍走流式，在本地装配成 chat.completion"""
    translator = StreamTranslator(str(payload.get("model") or ""))
    ended = False
    # 重新走一遍事件翻译（stream_chat 内部的 translator 是私有的），
    # 这里直接消费上游事件而不是消费 chat chunk，避免二次翻译丢状态。
    base = resolve_base_url(provider_base_url)
    body = build_responses_request(payload, cache_key=cache_key)
    headers = build_headers(
        credential.tokens.access_token, credential.identity.chatgpt_account_id
    )
    apply_session_headers(headers, cache_key)
    url = f"{base}/responses"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    detail = _safe_detail((await resp.aread()).decode("utf-8", "ignore"), credential)
                    if resp.status_code == 401:
                        raise CodexUpstreamError(
                            f"Codex 凭证被上游拒绝（401），请重新登录。详情：{detail}",
                            401, "codex_auth_failed",
                        )
                    raise CodexUpstreamError(
                        f"上游返回 {resp.status_code}: {detail}",
                        resp.status_code, f"upstream_{resp.status_code}",
                    )
                async for event in parse_sse_events(resp.aiter_lines()):
                    _, terminal, failed = translator.translate(event)
                    if failed:
                        raise CodexUpstreamError("Codex 上游生成失败", 502, "codex_response_failed")
                    if terminal:
                        ended = True
                        break
                if not ended:
                    raise CodexUpstreamError("Codex 响应在完成事件前中断", 502, "codex_incomplete_stream")
    except httpx.TimeoutException as exc:
        raise CodexUpstreamError(f"上游超时（{timeout_seconds}s）", 504, "timeout") from exc
    except httpx.HTTPError as exc:
        raise CodexUpstreamError(f"上游连接失败：{exc}", 502, "connect_failed") from exc

    result = translator.final_response()
    if "error" in result:
        raise CodexUpstreamError(
            str(result["error"].get("message") or "Codex 上游错误"), 502, "codex_upstream_error"
        )
    return result


# ==================== 剩余额度（wham usage） ====================


def wham_usage_url(provider_base_url: str) -> str:
    """额度接口地址。与 /responses 不同，wham 挂在 backend-api 根下不在 /codex 里

    官方后端：https://chatgpt.com/backend-api/wham/usage；
    自建中转（codex://host[/path]）按同样规律把末尾的 /codex 换成 /wham/usage，
    中转没实现的话查询会 404，status 会如实报「额度不可查」。
    """
    base = resolve_base_url(provider_base_url)
    prefix = base[:-len("/codex")] if base.endswith("/codex") else base
    return prefix + "/wham/usage"


def _window(raw: Any) -> Optional[Dict[str, Any]]:
    """限速窗口归一化（两种字段名拼写都认，与 codex2api 同口径）"""
    if not isinstance(raw, dict):
        return None
    def pick(*names, default=0):
        for n in names:
            if raw.get(n) is not None:
                return raw[n]
        return default
    return {
        "used_percent": float(pick("used_percent", "usedPercent", default=0) or 0),
        "window_seconds": int(pick("limit_window_seconds", "limitWindowSeconds", default=0) or 0),
        "reset_after_seconds": int(pick("reset_after_seconds", "resetAfterSeconds", default=0) or 0),
        "reset_at": int(pick("reset_at", "resetAt", default=0) or 0),
    }


def parse_wham_usage(body: Dict[str, Any]) -> Dict[str, Any]:
    """wham/usage 响应 → 剩余额度快照（5h 主窗口 + 7d 副窗口 + 重置券）"""
    rate = body.get("rate_limit") or {}
    credits = body.get("rate_limit_reset_credits") or {}
    subscription = (
        body.get("subscription_expires_at")
        or body.get("subscription_active_until")
        or body.get("chatgpt_subscription_active_until")
        or ""
    )
    result: Dict[str, Any] = {
        "email": body.get("email") or "",
        "plan_type": body.get("plan_type") or "",
        "subscription_expires_at": subscription,
        "allowed": bool(rate.get("allowed", True)),
        "limit_reached": bool(rate.get("limit_reached", False)),
        "primary_window": _window(rate.get("primary_window") or rate.get("primaryWindow")),
        "secondary_window": _window(rate.get("secondary_window") or rate.get("secondaryWindow")),
        "reset_credits": {
            "available_count": int(credits.get("available_count") or 0),
            "applicable_available_count": int(credits.get("applicable_available_count") or 0),
        } if credits else None,
    }
    return result


async def fetch_usage(
    credential: CodexCredential,
    provider_base_url: str,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """查账号剩余额度（GET wham/usage）。凭证失效时抛 CodexUpstreamError(codex_auth_failed)"""
    url = wham_usage_url(provider_base_url)
    headers = build_headers(
        credential.tokens.access_token, credential.identity.chatgpt_account_id,
        accept="application/json",
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CodexUpstreamError(f"额度查询连接失败：{exc}", 502, "connect_failed") from exc

    if resp.status_code != 200:
        detail = resp.text[:500]
        if resp.status_code == 401:
            raise CodexUpstreamError(
                f"Codex 凭证被上游拒绝（401），账号可能已掉线。详情：{detail}",
                401, "codex_auth_failed",
            )
        raise CodexUpstreamError(
            f"额度查询返回 {resp.status_code}: {detail}",
            resp.status_code, f"upstream_{resp.status_code}",
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise CodexUpstreamError("额度查询响应不是合法 JSON", 502, "bad_json") from exc
    return parse_wham_usage(body)


# ==================== 连通探测 ====================


async def probe_models(
    credential: CodexCredential,
    provider_base_url: str,
    timeout_seconds: int = 15,
) -> Dict[str, Any]:
    """拉账号真实的模型清单（GET /models），用于绑定模型配置前先确认能用什么

    响应 schema 会随 Codex 版本演进，这里防御性提取 id 列表，原文也一并带回。
    """
    base = resolve_base_url(provider_base_url)
    headers = build_headers(
        credential.tokens.access_token, credential.identity.chatgpt_account_id,
        accept="application/json",
    )
    # 真实 Codex CLI 调 /models?client_version=<ver>，缺这个参数上游直接 400
    url = f"{base}/models?client_version={CODEX_CLIENT_VERSION}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CodexUpstreamError(f"连接 Codex 后端失败：{exc}", 502, "connect_failed") from exc

    if resp.status_code != 200:
        detail = resp.text[:500]
        if resp.status_code == 401:
            raise CodexUpstreamError(
                f"Codex 凭证被上游拒绝（401），请重新登录。详情：{detail}",
                401, "codex_auth_failed",
            )
        raise CodexUpstreamError(
            f"上游返回 {resp.status_code}: {detail}", resp.status_code, f"upstream_{resp.status_code}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise CodexUpstreamError("模型清单不是合法 JSON", 502, "bad_json") from exc

    return {"models": _extract_model_ids(body), "model_cards": _extract_model_cards(body), "raw": body}


def _extract_model_ids(body: Any) -> list:
    """从清单里抠出模型 slug（上游 schema：{"models": [{"slug": ...}]}）

    防御性提取：认 models[].slug，兜底再扫 id/slug 字段。
    """
    found: list = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("slug", "id"):
                value = node.get(key)
                if isinstance(value, str) and value:
                    found.append(value)
                    break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(body, dict) and isinstance(body.get("models"), list):
        for item in body["models"]:
            if isinstance(item, dict):
                value = item.get("slug") or item.get("id")
                if isinstance(value, str) and value:
                    found.append(value)
        return list(dict.fromkeys(found))
    walk(body)
    return list(dict.fromkeys(found))


def _extract_model_cards(body: Any) -> list:
    """模型卡片（绑定模型配置时用：展示名与上下文窗口）"""
    cards = []
    items = body.get("models") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return cards
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug") or item.get("id")
        if not isinstance(slug, str) or not slug:
            continue
        cards.append({
            "slug": slug,
            "display_name": item.get("display_name") or slug,
            "context_window": item.get("context_window"),
            "max_context_window": item.get("max_context_window"),
        })
    return cards
