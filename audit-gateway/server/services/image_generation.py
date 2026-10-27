"""公司托管生图：复用已登记渠道与模型白名单，供应商凭据不离开网关。"""
from __future__ import annotations

import base64
import binascii
import io
import re
from typing import Literal

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from services.forwarding import UpstreamError, UpstreamTarget, chat_completions_url

# 本期实际消费者的协议清单；按真实上游名匹配，别名只用于对外访问。
IMAGE_MODELS = {
    "gpt-image-2": "images",
    "gpt-image-2-t": "images",
    "gemini-3.1-flash-image-preview": "gemini",
    "doubao-seedream-5-0-260128": "seedream",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SIZES = {"1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3"}


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field("", max_length=128)
    prompt: str = Field(..., min_length=1, max_length=16000)
    size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024"
    n: Literal[1] = 1
    response_format: Literal["b64_json"] = "b64_json"
    reference_images: list[str] = Field(default_factory=list, max_length=3)
    session_id: str = Field("", max_length=128)


def is_image_model(model) -> bool:
    return model.model_name in IMAGE_MODELS


def decode_image(value: str, *, data_url: bool = False) -> tuple[bytes, str]:
    """只接受内联图片，不让网关访问用户指定的 URL。"""
    if data_url:
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)", value)
        if not match:
            raise ValueError("参考图须为内联 PNG/JPEG/WebP 图片")
        value = match.group(2)
    if len(value) > (MAX_IMAGE_BYTES * 4 // 3) + 1024:
        raise ValueError("单张图片超过 8 MiB 上限")
    try:
        raw = base64.b64decode(value, validate=True)
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("图片为空或超过 8 MiB 上限")
        with Image.open(io.BytesIO(raw)) as img:
            kind = img.format
            if img.width * img.height > 40_000_000:
                raise ValueError("图片像素超过上限")
            if kind not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("只支持 PNG/JPEG/WebP 图片")
            img.verify()
    except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片编码或文件内容无效") from exc
    return raw, {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[kind]


def normalize_result(payload: dict, *, protocol: str = "images") -> dict:
    entries = payload.get("data")
    encoded = entries[0].get("b64_json") if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
    if protocol == "gemini":
        choices = payload.get("choices")
        message = choices[0].get("message", {}) if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        candidates = [content] if isinstance(content, str) else []
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict):
                image = part.get("image_url")
                candidates.append(image.get("url") if isinstance(image, dict) else image)
                candidates.append(part.get("text"))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            # OpenAI 兼容接口可能返回原始 base64、内联 URL 或 Markdown 内联图。
            match = re.search(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", candidate)
            value = match.group(1) if match else candidate.strip()
            try:
                decode_image(value)
            except ValueError:
                continue
            encoded = value
            break
    if not isinstance(encoded, str):
        raise UpstreamError("生图上游没有返回内联图片", 502, "image_missing")
    try:
        raw, mime = decode_image(encoded)
    except ValueError as exc:
        raise UpstreamError(str(exc), 502, "image_invalid") from exc
    result = {"data": [{"b64_json": base64.b64encode(raw).decode("ascii"), "mime_type": mime}]}
    if isinstance(payload.get("usage"), dict):
        result["usage"] = payload["usage"]
    return result


async def generate(target: UpstreamTarget, body: ImageRequest) -> dict:
    if target.protocol != "openai" or target.model_name not in IMAGE_MODELS:
        raise UpstreamError("该模型不支持当前生图协议", 400, "image_model_unsupported")
    refs = [decode_image(value, data_url=True) for value in body.reference_images]
    base = chat_completions_url(target.base_url).removesuffix("/chat/completions")
    headers = {"Authorization": f"Bearer {target.api_key}"}
    fields = {"model": target.model_name, "prompt": body.prompt,
              "size": body.size, "n": 1, "response_format": "b64_json"}
    protocol = IMAGE_MODELS[target.model_name]
    if protocol == "gemini":
        url = f"{base}/chat/completions"
        content = [{"type": "text", "text": body.prompt}] + [
            {"type": "image_url", "image_url": {"url": ref}} for ref in body.reference_images]
        kwargs = {"json": {"model": target.model_name, "messages": [{"role": "user", "content": content}],
            "stream": False, "extra_body": {"google": {"image_config": {
                "aspect_ratio": SIZES[body.size], "image_size": "1K"}}}}}
    elif protocol == "seedream":
        url = f"{base}/images/generations"
        # Seedream 使用 JSON 内联参考图，不能套用 GPT 的 multipart 编辑接口。
        fields = {"model": target.model_name, "prompt": body.prompt, "response_format": "b64_json",
            "size": {"1024x1024": "2048x2048", "1536x1024": "2496x1664", "1024x1536": "1664x2496"}[body.size],
            "stream": False, "output_format": "png", "watermark": True}
        if refs:
            fields["image"] = body.reference_images
        kwargs = {"json": fields}
    elif refs:
        url = f"{base}/images/edits"
        kwargs = {"data": {key: str(value) for key, value in fields.items()},
                  "files": [("image", (f"reference-{i}." + {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime], raw, mime)) for i, (raw, mime) in enumerate(refs)]}
    else:
        url = f"{base}/images/generations"
        kwargs = {"json": fields}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(target.timeout_seconds, connect=10), trust_env=False) as client:
            async with client.stream("POST", url, headers=headers, **kwargs) as response:
                if response.is_error:
                    # 不透传上游正文，避免厂商回显密钥、账号或内部地址。
                    status = 429 if response.status_code == 429 else 502
                    raise UpstreamError(f"生图上游返回 HTTP {response.status_code}", status, "image_upstream_error")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise UpstreamError("生图上游响应超过上限", 502, "image_response_too_large")
                import json
                payload = json.loads(chunks)
                if not isinstance(payload, dict) or payload.get("error"):
                    raise UpstreamError("生图上游返回错误响应", 502, "image_upstream_error")
                try:
                    return normalize_result(payload, protocol=protocol)
                except UpstreamError as exc:
                    # 图片校验失败也保留上游已经返回的 usage，费用不能事后凭空补。
                    exc.usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
                    raise
    except httpx.TimeoutException as exc:
        raise UpstreamError("生图超时，请稍后重试", 504, "image_timeout") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError("生图上游连接或响应异常", 502, "image_upstream_error") from exc
