"""上游转发

流式与非流式走同一套「取模型 → 校验白名单 → 脱敏 → 发上游 → 记账」流程，
区别只在响应怎么读。

关于流式的 usage：OpenAI 兼容接口默认**不会**在 SSE 里返回 token 用量，
必须显式带 `stream_options: {"include_usage": true}`，最后一个 chunk 才带 usage。
网关强制加上这个参数——用量统计是硬需求，不能指望客户端记得传。
上游若不支持该参数，退化成按字符估算并打 tokens_estimated 标记。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

import httpx
from loguru import logger
from model.database import ModelInfo, UpstreamProvider
from utils.secret_box import SecretDecryptError, SecretPurpose, decrypt_secret

# SSE 结束标记
SSE_DONE = "data: [DONE]\n\n"


class UpstreamError(RuntimeError):
    """上游不可用或返回错误"""

    def __init__(self, message: str, status_code: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass
class UpstreamTarget:
    """一次调用的上游目标"""

    provider_id: int
    base_url: str
    api_key: str
    timeout_seconds: int
    model_name: str
    model_id: int
    # base_url 为 codex:// 哨兵时走 Codex 适配层（services/codex_channel/），
    # 其余走标准 OpenAI 兼容转发
    protocol: str = "openai"
    # 网关审计会话 id，只用于派生上游会话键，绝不外发原始值
    session_id: str = ""


def build_target(
    provider: UpstreamProvider, model: ModelInfo, session_id: str = ""
) -> UpstreamTarget:
    """解出上游地址与 Key

    Key 在库里是加密的，这里解密后只在内存里传递，绝不写日志。
    Codex 渠道（base_url 形如 codex://chatgpt）的 api_key 密文里存的是
    OAuth 凭证 JSON，同样只在内存里解出。
    """
    if provider.status != 1:
        raise UpstreamError(f"上游渠道「{provider.name}」已停用", 503, "provider_disabled")

    if not provider.api_key_encrypted:
        raise UpstreamError(
            f"上游渠道「{provider.name}」未配置 API Key", 503, "provider_no_key"
        )

    try:
        api_key = decrypt_secret(provider.api_key_encrypted, SecretPurpose.PROVIDER)
    except SecretDecryptError as exc:
        # 多半是换了 PROVIDER_ENCRYPTION_KEY 又没做重加密
        raise UpstreamError(
            f"上游渠道「{provider.name}」的 Key 解密失败：{exc}", 500, "key_decrypt_failed"
        ) from exc

    return UpstreamTarget(
        provider_id=provider.id,
        base_url=provider.base_url.rstrip("/"),
        api_key=api_key,
        timeout_seconds=provider.timeout_seconds or 60,
        model_name=model.model_name,
        model_id=model.id,
        protocol="codex" if provider.base_url.lower().startswith("codex://") else "openai",
        session_id=session_id or "",
    )


def prepare_payload(body: Dict[str, Any], target: UpstreamTarget) -> Dict[str, Any]:
    """整理转发给上游的请求体

    - model 换成上游真实模型名（客户端拿到的可能是别名）
    - 流式强制带 include_usage，否则拿不到 token 数
    - 去掉客户端可能塞进来的越权字段
    """
    payload = dict(body)
    payload["model"] = target.model_name

    if payload.get("stream"):
        options = dict(payload.get("stream_options") or {})
        options["include_usage"] = True
        payload["stream_options"] = options

    # 这些字段由网关决定，不接受客户端指定
    for field in ("api_key", "base_url", "user_uid"):
        payload.pop(field, None)

    return payload


def chat_completions_url(base_url: str) -> str:
    """拼出上游的 chat/completions 地址

    供应商登记 base_url 时有两种写法，都得支持：

    - 只写到域名（``https://api.deepseek.com``）—— 要补 ``/v1``；
    - 已经带了 API 版本段（火山方舟的 ``…/api/v3``、自建代理的 ``…/v1``）
      —— 再补一个 ``/v1`` 就成了 ``/api/v3/v1/chat/completions``，上游一律 404。

    2026-09-11 线上就是这样断的：豆包挪到方舟直连后每次调用都是「上游返回 404」，
    而客户端只能看到这一句，看不出是路径拼错了。
    """
    base = (base_url or "").rstrip("/")
    if re.search(r"/v\d+[a-z0-9.-]*$", base, re.IGNORECASE):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _headers(target: UpstreamTarget) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {target.api_key}",
        "Content-Type": "application/json",
    }


# ==================== Codex 渠道适配 ====================
#
# Codex 渠道在库里登记的是 codex:// 哨兵地址，请求要翻译成 Codex Responses
# 协议发给 chatgpt.com/backend-api/codex，响应再翻回 chat/completions。
# 翻译与凭证续期都在 services/codex_channel/，这里只负责按 protocol 分流，
# 让上层（审计/记账/脱敏）看到的仍然是标准 OpenAI 兼容契约。


def _note_codex_failure(target: UpstreamTarget, exc) -> None:
    """凭证类失败顺手记进掉号检测（其余错误是网络/限速，不算掉号）"""
    if getattr(exc, "code", "") in ("codex_auth_failed", "codex_login_required"):
        from services.codex_channel.health import mark_dead

        mark_dead(target.provider_id, str(exc))


def _codex_cache_key(target: UpstreamTarget) -> str:
    """审计会话 id → 上游会话键

    只发哈希不发原始 id：同一会话的多轮请求落到同一个 prompt_cache_key 上
    （缓存复用），不同会话不同键（缓存隔离），而内部会话号不出网关。
    """
    if not target.session_id:
        return ""
    import hashlib

    return hashlib.sha256(("codex-sess:" + target.session_id).encode()).hexdigest()[:32]


async def _codex_credential(target: UpstreamTarget):
    """解出 Codex 凭证并保证 access_token 可用（必要时续期写回）"""
    from services.codex_channel import CodexCredential, ensure_fresh
    from services.codex_channel.credentials import CredentialError

    try:
        cred = CodexCredential.from_json(target.api_key)
    except CredentialError as exc:
        raise UpstreamError(str(exc), 503, "codex_bad_credential") from exc

    try:
        access_token, cred = await ensure_fresh(target.provider_id, cred)
    except Exception as exc:
        from services.codex_channel import OAuthError, OAuthLoginRequired
        from services.codex_channel.health import mark_dead

        if isinstance(exc, OAuthLoginRequired):
            # 掉号：被动标记，后台 status 立刻能看到
            mark_dead(target.provider_id, str(exc))
            raise UpstreamError(str(exc), 503, "codex_login_required") from exc
        if isinstance(exc, OAuthError):
            raise UpstreamError("Codex 凭据续期暂不可用，请稍后重试", 502, "codex_refresh_unavailable") from exc
        raise
    return access_token, cred


async def call_upstream(
    target: UpstreamTarget, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """非流式调用，返回上游 JSON"""
    if target.protocol == "codex":
        return await _codex_complete(target, payload)

    url = chat_completions_url(target.base_url)

    try:
        async with httpx.AsyncClient(timeout=target.timeout_seconds) as client:
            resp = await client.post(url, headers=_headers(target), json=payload)
    except httpx.TimeoutException as exc:
        raise UpstreamError(f"上游超时（{target.timeout_seconds}s）", 504, "timeout") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"上游连接失败：{exc}", 502, "connect_failed") from exc

    if resp.status_code != 200:
        detail = resp.text[:500]
        logger.warning(f"上游返回 {resp.status_code}: {detail}")
        raise UpstreamError(
            f"上游返回 {resp.status_code}", resp.status_code, f"upstream_{resp.status_code}"
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamError("上游返回的不是合法 JSON", 502, "bad_json") from exc


def extract_text(payload: Dict[str, Any]) -> str:
    """从非流式响应里取出回复正文，用于审计存档"""
    choices = payload.get("choices") or []
    parts = []
    for choice in choices:
        message = (choice or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def parse_sse_line(line: str) -> Optional[Dict[str, Any]]:
    """解析一行 SSE。非数据行、[DONE]、坏 JSON 都返回 None"""
    if not line.startswith("data:"):
        return None

    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None

    try:
        parsed = json.loads(data)
    except ValueError:
        return None

    return parsed if isinstance(parsed, dict) else None


def collect_delta(chunk: Dict[str, Any]) -> str:
    """从流式 chunk 里取增量文本"""
    choices = chunk.get("choices") or []
    parts = []
    for choice in choices:
        delta = (choice or {}).get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts)


async def _codex_complete(target: UpstreamTarget, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Codex 非流式：上游仍走流式，适配层装配成 chat.completion"""
    from services.codex_channel import CodexUpstreamError, complete_chat

    _, cred = await _codex_credential(target)
    try:
        result = await complete_chat(
            cred, target.base_url, payload,
            cache_key=_codex_cache_key(target),
            timeout_seconds=target.timeout_seconds,
        )
    except CodexUpstreamError as exc:
        _note_codex_failure(target, exc)
        status = 503 if exc.code in {"codex_auth_failed", "codex_login_required"} else exc.status_code
        raise UpstreamError(str(exc), status, exc.code) from exc
    from services.codex_channel.health import mark_alive
    mark_alive(target.provider_id)
    return result


async def _codex_stream(
    target: UpstreamTarget, payload: Dict[str, Any]
) -> AsyncGenerator[Tuple[str, Optional[Dict[str, Any]]], None]:
    """Codex 流式：翻译后的 chat chunk 行，契约与标准流式一致"""
    from services.codex_channel import CodexUpstreamError, stream_chat

    _, cred = await _codex_credential(target)
    try:
        async for line, chunk in stream_chat(
            cred, target.base_url, payload,
            cache_key=_codex_cache_key(target),
            timeout_seconds=target.timeout_seconds,
        ):
            yield line, chunk
        from services.codex_channel.health import mark_alive
        mark_alive(target.provider_id)
    except CodexUpstreamError as exc:
        _note_codex_failure(target, exc)
        status = 503 if exc.code in {"codex_auth_failed", "codex_login_required"} else exc.status_code
        raise UpstreamError(str(exc), status, exc.code) from exc


async def stream_upstream(
    target: UpstreamTarget, payload: Dict[str, Any]
) -> AsyncGenerator[Tuple[str, Optional[Dict[str, Any]]], None]:
    """流式调用

    逐行 yield (原始 SSE 行, 解析后的 chunk)：
    原始行**原样透传**给客户端（不重新序列化，避免破坏上游的字段与顺序），
    解析结果只用于旁路统计。
    """
    if target.protocol == "codex":
        async for item in _codex_stream(target, payload):
            yield item
        return

    url = chat_completions_url(target.base_url)

    try:
        async with httpx.AsyncClient(timeout=target.timeout_seconds) as client:
            async with client.stream(
                "POST", url, headers=_headers(target), json=payload
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "ignore")[:500]
                    logger.warning(f"上游流式返回 {resp.status_code}: {detail}")
                    raise UpstreamError(
                        f"上游返回 {resp.status_code}",
                        resp.status_code,
                        f"upstream_{resp.status_code}",
                    )

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    yield line, parse_sse_line(line)

    except httpx.TimeoutException as exc:
        raise UpstreamError(f"上游超时（{target.timeout_seconds}s）", 504, "timeout") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"上游连接失败：{exc}", 502, "connect_failed") from exc


def error_sse(message: str) -> str:
    """把错误包成 SSE 事件

    流式响应头已经发出去了，没法再改 HTTP 状态码，
    只能用一个 error 事件告诉客户端出了什么事。
    """
    body = json.dumps({"error": {"message": message, "type": "gateway_error"}},
                      ensure_ascii=False)
    return f"data: {body}\n\n"
