"""管理员模型发现：只拉目录，不调用付费生成。"""
from urllib.parse import urlsplit
import json
import httpx
from services.forwarding import UpstreamError
from services.image_generation import IMAGE_MODELS
from utils.secret_box import decrypt_secret, SecretPurpose

IMAGE_DETAILS = {
    "gpt-image-2": {"title": "GPT 图像（按次）", "protocol": "图片生成 / 编辑", "resolution": "约 1K"},
    "gpt-image-2-t": {"title": "GPT 图像（按 token）", "protocol": "图片生成 / 编辑", "resolution": "约 1K"},
    "gemini-3.1-flash-image-preview": {"title": "Gemini 闪电生图", "protocol": "对话式生图", "resolution": "1K"},
    "doubao-seedream-5-0-260128": {"title": "豆包 Seedream 生图", "protocol": "图片生成 / 参考图", "resolution": "约 2K"},
}

async def discover(provider):
    if provider.status != 1 or not provider.api_key_encrypted:
        raise UpstreamError("请先启用渠道并配置 Key", 400)
    base = provider.base_url.rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise UpstreamError("目录探测需要有效的 HTTP/HTTPS 渠道地址", 400)
    url = base + ("/models" if parts.path.rstrip("/").endswith("/v1") else "/v1/models")
    key = decrypt_secret(provider.api_key_encrypted, SecretPurpose.PROVIDER)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(min(provider.timeout_seconds or 30, 30), connect=10), trust_env=False) as client:
            async with client.stream("GET", url, headers={"Authorization": "Bearer " + key}) as response:
                if response.is_error:
                    raise UpstreamError(f"模型目录返回 HTTP {response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise UpstreamError("模型目录响应超过上限")
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise UpstreamError("模型目录格式无效")
        ids = sorted({entry["id"] for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            and 0 < len(entry["id"]) <= 128 and not any(c.isspace() for c in entry["id"])})
        if len(ids) > 1000:
            raise UpstreamError("模型目录条目超过上限")
        return [{"id": name, "supported": name in IMAGE_MODELS,
            **IMAGE_DETAILS.get(name, {"title": name, "protocol": "暂不支持", "resolution": "—"}),
            "max_reference_images": 3 if name in IMAGE_MODELS else 0} for name in ids]
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError("模型目录连接失败或响应无效") from exc
