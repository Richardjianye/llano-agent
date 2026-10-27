from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from mcp_adapters.base import Capability, McpAdapter
from utils.errors import ValidationError_

BASE_URL = "https://openapi.lingxing.com"
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()


def _sign_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def make_signature(parameters: Mapping[str, Any], app_id: str) -> str:
    """按领星文档生成签名：ASCII 排序 → MD5 大写 → AES/ECB/PKCS7。"""
    pairs = [
        f"{key}={_sign_value(parameters[key])}"
        for key in sorted(parameters)
        if parameters[key] != ""
    ]
    digest = hashlib.md5("&".join(pairs).encode("utf-8")).hexdigest().upper()
    key = app_id.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise ValidationError_("领星 AppId 长度不符合 AES 密钥要求")
    padder = PKCS7(128).padder()
    padded = padder.update(digest.encode("ascii")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


class LingxingAdapter(McpAdapter):
    key = "lingxing"
    display_name = "领星 ERP"
    description = "领星开放平台只读接口；AppSecret 只在网关服务端解密使用。"
    permission_mode = "local_grants"
    credential_platform = None

    def __init__(self) -> None:
        data_path = Path(__file__).with_name("data") / "lingxing.json"
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        self._capabilities = tuple(
            Capability(
                code=row["code"], name=row["name"], domain=row["domain"],
                method=row["method"], path=row["path"],
                input_schema=row["input_schema"], source_hash=row["source_hash"],
                metadata={"doc_url": row["doc_url"], "token_bucket": row["token_bucket"]},
            )
            for row in raw["capabilities"]
        )

    def config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "app_id", "label": "App ID", "type": "text", "required": True, "secret": True},
            {"key": "app_secret", "label": "App Secret", "type": "password", "required": True, "secret": True},
            {"key": "proxy_url", "label": "正向代理 URL", "type": "password", "required": False, "secret": True, "help": "HTTP CONNECT 代理；用户名与密码只保存在网关密文配置中"},
            {"key": "timeout_seconds", "label": "请求超时（秒）", "type": "number", "required": False, "secret": False, "default": 30, "min": 3, "max": 120},
        ]

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities

    def validate_config(self, auth: Mapping[str, Any], config: Mapping[str, Any]) -> None:
        if not str(auth.get("app_id", "")).strip() or not str(auth.get("app_secret", "")).strip():
            raise ValidationError_("领星实例必须配置 App ID 与 App Secret")
        timeout = int(config.get("timeout_seconds", 30))
        if timeout < 3 or timeout > 120:
            raise ValidationError_("timeout_seconds 必须在 3 到 120 之间")

        proxy_url = str(auth.get("proxy_url", "")).strip()
        if proxy_url:
            parsed = urlsplit(proxy_url)
            if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
                raise ValidationError_("正向代理必须是带主机和端口的 http:// URL")

    async def _token(self, auth: Mapping[str, Any], timeout: int) -> str:
        cache_key = hashlib.sha256(
            f"{auth['app_id']}|{auth.get('proxy_url', '')}".encode("utf-8")
        ).hexdigest()
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > time.monotonic() + 60:
            return cached[0]
        async with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
            if cached and cached[1] > time.monotonic() + 60:
                return cached[0]
            async with httpx.AsyncClient(timeout=timeout, proxy=auth.get("proxy_url") or None) as client:
                response = await client.post(
                    f"{BASE_URL}/api/auth-server/oauth/access-token",
                    files={"appId": (None, str(auth["app_id"])), "appSecret": (None, str(auth["app_secret"]))},
                )
            response.raise_for_status()
            payload = response.json()
            token = (payload.get("data") or {}).get("access_token")
            if str(payload.get("code")) != "200" or not token:
                raise RuntimeError(f"领星令牌获取失败：{payload.get('msg') or payload.get('message') or '未知错误'}")
            expires = max(120, int((payload.get("data") or {}).get("expires_in") or 7199))
            _TOKEN_CACHE[cache_key] = (str(token), time.monotonic() + expires)
            return str(token)

    async def execute(
        self, capability: Capability, arguments: Mapping[str, Any],
        auth: Mapping[str, Any], config: Mapping[str, Any],
    ) -> Any:
        self.validate_config(auth, config)
        timeout = int(config.get("timeout_seconds", 30))
        access_token = await self._token(auth, timeout)
        common = {
            "access_token": access_token,
            "app_key": str(auth["app_id"]),
            "timestamp": str(int(time.time())),
        }
        sign = make_signature({**common, **dict(arguments)}, str(auth["app_id"]))
        query = {**common, "sign": sign}
        async with httpx.AsyncClient(timeout=timeout, proxy=auth.get("proxy_url") or None) as client:
            if capability.method == "GET":
                response = await client.get(f"{BASE_URL}{capability.path}", params={**query, **dict(arguments)})
            elif capability.method == "POST":
                response = await client.post(f"{BASE_URL}{capability.path}", params=query, json=dict(arguments))
            else:
                raise RuntimeError("受信任只读目录包含不支持的方法")
        response.raise_for_status()
        return response.json()

    async def health(self, auth: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_config(auth, config)
        started = time.perf_counter()
        await self._token(auth, int(config.get("timeout_seconds", 30)))
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000)}
