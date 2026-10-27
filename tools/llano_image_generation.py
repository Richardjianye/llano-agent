"""品牌桌面生图只使用托管网关，沿用 image_generate 的文件返回契约。"""
from __future__ import annotations

import base64
import io
import json
import os
import uuid
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def managed_image_policy():
    from hermes_cli.product_policy import ProductPolicyState, load_product_policy_status

    status = load_product_policy_status()
    policy = status.policy
    if status.state is ProductPolicyState.ACTIVE and policy and policy.managed_image_generation and policy.gateway_origin:
        return policy
    return None


def available() -> bool:
    policy = managed_image_policy()
    if not policy:
        return False
    key = os.environ.get("LLANO_GATEWAY_API_KEY", "").strip()
    if not key:
        return False
    try:
        import httpx
        # 注册表缓存 check_fn；只给真正获授权的用户发送生图 schema。
        with httpx.Client(timeout=3, trust_env=False) as client:
            response = client.get(f"{policy.gateway_origin.rstrip('/')}/v1/images/models",
                headers={"Authorization": "Bearer " + key})
            response.raise_for_status()
            payload = response.json()
            return isinstance(payload, dict) and isinstance(payload.get("data"), list) and bool(payload["data"])
    except Exception:
        return False


def generate_image(prompt: str, aspect_ratio: str, *, image_url=None, reference_image_urls=None, model=None) -> str:
    policy = managed_image_policy()
    if not policy:
        return json.dumps({"success": False, "error": "capability_unavailable"})
    key = os.environ.get("LLANO_GATEWAY_API_KEY", "").strip()
    if not key:
        return json.dumps({"success": False, "error": "请先登录公司网关"}, ensure_ascii=False)
    try:
        import httpx
        from PIL import Image
    except ImportError:
        return json.dumps({"success": False, "error": "当前运行环境缺少生图依赖，请更新工作台"}, ensure_ascii=False)
    refs = ([image_url] if image_url else []) + list(reference_image_urls or [])
    if len(refs) > 3 or any(not isinstance(ref, str) or not ref.startswith("data:image/") for ref in refs):
        return json.dumps({"success": False, "error": "最多使用 3 张本机或内联参考图；请先下载远程图片"}, ensure_ascii=False)
    sizes = {"square": "1024x1024", "1:1": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
    size = sizes.get(aspect_ratio)
    if size is None:
        try:
            width, height = (int(part) for part in aspect_ratio.split(":"))
            size = "1536x1024" if width > height else "1024x1536" if width < height else "1024x1024"
        except (ValueError, AttributeError):
            return json.dumps({"success": False, "error": "图片比例无效"}, ensure_ascii=False)
    body = {"prompt": prompt, "size": size, "reference_images": refs}
    if model:
        body["model"] = model
    try:
        # 仅发送设备 Key 给编译期固化的网关；上游 Key 绝不进入客户端。
        with httpx.Client(timeout=httpx.Timeout(630, connect=10), trust_env=False) as client:
            with client.stream("POST", f"{policy.gateway_origin.rstrip('/')}/v1/images/generations",
                               headers={"Authorization": f"Bearer {key}"}, json=body) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 16 * 1024 * 1024:
                        raise ValueError("生图响应超过上限")
                payload = json.loads(data)
                if response.is_error:
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    message = error.get("message") if isinstance(error, dict) else None
                    return json.dumps({"success": False, "error": message or f"生图失败（HTTP {response.status_code}）"}, ensure_ascii=False)
        encoded = payload["data"][0]["b64_json"]
        if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES * 4 // 3) + 1024:
            raise ValueError("图片超过上限")
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            kind = image.format
            if image.width * image.height > 40_000_000:
                raise ValueError("图片像素超过上限")
            image.verify()
        suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(kind)
        if not suffix:
            raise ValueError("图片格式无效")
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home()) / "cache" / "images"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"llano-{uuid.uuid4().hex}{suffix}"
        path.write_bytes(raw)
        return json.dumps({"success": True, "image": str(path), "model": payload.get("model"),
                           "provider": "公司 AI 网关"}, ensure_ascii=False)
    except httpx.TimeoutException:
        return json.dumps({"success": False, "error": "生图超时，请稍后重试"}, ensure_ascii=False)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, OSError, Image.DecompressionBombError):
        return json.dumps({"success": False, "error": "生图连接失败或图片响应无效，请稍后重试"}, ensure_ascii=False)
