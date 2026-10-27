from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import httpx

from mcp_adapters.base import Capability, McpAdapter
from utils.errors import McpUpstreamError, PermissionError_, ValidationError_


AUTH_BASE_URL = "http://auth.example.invalid"
API_BASE_URL = "http://data.example.invalid/llano"
ROOT_APP_ID = "00000000-0000-4000-8000-000000000001"
ROOT_REDIRECT_URL = "http://portal.example.invalid/home"
CLIENT_ID = "llano-gateway"


@dataclass(frozen=True)
class CachedToken:
    value: str
    expires_at: float


def _source_hash(row: Mapping[str, Any]) -> str:
    trusted = {
        "code": row["code"],
        "method": row["method"],
        "path": row["path"],
        "input_schema": row["input_schema"],
        "metadata": row["metadata"],
    }
    encoded = json.dumps(
        trusted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InternalDatahubAdapter(McpAdapter):
    key = "internal_datahub"
    display_name = "自有数据中台"
    description = "每位用户使用自己的中台账号调用受信任只读接口。"
    permission_mode = "upstream_account"
    credential_platform = "internal_datahub"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._token_cache: dict[str, CachedToken] = {}
        self._token_lock = asyncio.Lock()
        data_path = Path(__file__).with_name("data") / "internal_datahub.json"
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        self._capabilities = tuple(
            Capability(
                code=row["code"],
                name=row["name"],
                domain=row["domain"],
                method=row["method"],
                path=row["path"],
                input_schema=row["input_schema"],
                source_hash=_source_hash(row),
                metadata=row["metadata"],
            )
            for row in raw["capabilities"]
        )

    def config_schema(self) -> list[dict[str, Any]]:
        return [{
            "key": "timeout_seconds",
            "label": "请求超时（秒）",
            "type": "number",
            "required": False,
            "secret": False,
            "default": 30,
            "min": 3,
            "max": 120,
        }]

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities

    def validate_config(
        self, auth: Mapping[str, Any], config: Mapping[str, Any]
    ) -> None:
        unknown_auth = set(auth) - {"account", "password"}
        if unknown_auth:
            raise ValidationError_("自有中台凭据包含不支持的字段")
        has_account = bool(str(auth.get("account", "")).strip())
        has_password = bool(str(auth.get("password", "")))
        if has_account != has_password:
            raise ValidationError_("中台账号与密码必须同时提供")
        timeout = int(config.get("timeout_seconds", 30))
        if timeout < 3 or timeout > 120:
            raise ValidationError_("timeout_seconds 必须在 3 到 120 之间")

    @staticmethod
    def _credentials(auth: Mapping[str, Any]) -> tuple[str, str]:
        account = str(auth.get("account", "")).strip()
        password = str(auth.get("password", ""))
        if not account or not password:
            raise ValidationError_("请先绑定并测试自己的中台账号")
        return account, password

    @staticmethod
    def _cache_key(account: str, password: str) -> str:
        return hashlib.sha256(f"{account}\0{password}".encode("utf-8")).hexdigest()

    async def _post_json(
        self,
        url: str,
        *,
        timeout: int,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self._transport
            ) as client:
                response = await client.post(url, json=dict(json_body), headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise McpUpstreamError(
                "datahub_timeout", "中台请求超时，请稍后重试", http_status=504
            ) from exc
        except httpx.RequestError as exc:
            raise McpUpstreamError(
                "datahub_connect_failed", "无法连接中台，请稍后重试"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise McpUpstreamError(
                "datahub_upstream_http_error", "中台服务返回异常状态，请稍后重试"
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise McpUpstreamError(
                "datahub_invalid_response", "中台返回了无法识别的数据"
            ) from exc
        if not isinstance(payload, dict):
            raise McpUpstreamError(
                "datahub_invalid_response", "中台返回了无法识别的数据"
            )
        return payload

    async def _login(
        self,
        auth: Mapping[str, Any],
        timeout: int,
        *,
        force: bool = False,
    ) -> str:
        account, password = self._credentials(auth)
        cache_key = self._cache_key(account, password)
        cached = self._token_cache.get(cache_key)
        if not force and cached and cached.expires_at > time.monotonic() + 60:
            return cached.value

        async with self._token_lock:
            cached = self._token_cache.get(cache_key)
            if not force and cached and cached.expires_at > time.monotonic() + 60:
                return cached.value

            request_cid = datetime.now().strftime("%Y%m%d")
            payload = await self._post_json(
                f"{AUTH_BASE_URL}/sso/SSOLogin",
                timeout=timeout,
                json_body={
                    "Phone": account,
                    "Password": base64.b64encode(password.encode("utf-8")).decode("ascii"),
                    "LoginType": "password",
                    "ClientIp": "",
                    "AppId": ROOT_APP_ID,
                    "RedirectUrl": ROOT_REDIRECT_URL,
                    "RequestCid": request_cid,
                    "IsReturnSubordinate": True,
                    "Ati": CLIENT_ID,
                    "Eid": CLIENT_ID,
                    "ImgCodeId": "",
                    "Token": "",
                },
            )
            data = payload.get("data") or payload.get("Data") or {}
            if not isinstance(data, dict):
                data = {}
            if data.get("IsNeedImgCode") or data.get("IsNeedSecondFactor"):
                raise McpUpstreamError(
                    "datahub_interactive_verification_required",
                    "中台要求验证码或二次验证，请在中台完成验证后再试",
                )
            if data.get("IsUpdatePassword"):
                raise McpUpstreamError(
                    "datahub_password_update_required",
                    "中台要求更新密码，请先在中台完成密码更新",
                )
            token = data.get("Token") or data.get("token")
            if payload.get("IsSuccess") is not True or not token:
                raise McpUpstreamError(
                    "datahub_auth_failed", "中台账号或密码验证失败"
                )

            expires_in = data.get("ExpiresIn")
            if expires_in is None:
                expires_in = data.get("expires_in")
            if expires_in is None:
                expires_in = 1200
            try:
                ttl_seconds = max(0, min(int(expires_in), 1200))
            except (TypeError, ValueError):
                ttl_seconds = 1200
            self._token_cache[cache_key] = CachedToken(
                value=str(token), expires_at=time.monotonic() + ttl_seconds
            )
            return str(token)

    async def execute(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        auth: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Any:
        self.validate_config(auth, config)
        if capability not in self._capabilities:
            raise PermissionError_("请求能力不属于自有中台受信任目录")
        if capability.method != "POST" or capability.metadata.get("read_only") is not True:
            raise PermissionError_("受信任目录包含不允许执行的接口")

        timeout = int(config.get("timeout_seconds", 30))
        token = await self._login(auth, timeout)
        request_cid = datetime.now().strftime("%Y%m%d")
        payload = await self._post_json(
            f"{API_BASE_URL}{capability.path}",
            timeout=timeout,
            json_body=arguments,
            headers={
                "Token": token,
                "AppId": str(capability.metadata["app_id"]),
                "requestCid": request_cid,
                "systemType": str(capability.metadata["system_type"]),
                "Ip": "",
                "Content-Type": "application/json",
            },
        )
        if str(payload.get("code")) != "0000":
            raise McpUpstreamError(
                "datahub_upstream_rejected",
                "中台拒绝了本次只读请求，请检查账号权限或稍后重试",
            )
        return payload

    async def health(
        self, auth: Mapping[str, Any], config: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.validate_config(auth, config)
        started = time.perf_counter()
        await self._login(auth, int(config.get("timeout_seconds", 30)), force=True)
        return {
            "ok": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
