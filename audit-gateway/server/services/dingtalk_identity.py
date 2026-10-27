"""钉钉网页登录授权码交换与企业成员身份校验。"""

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
from config import get_settings

AUTH_URL = "https://login.dingtalk.com/oauth2/auth"
API_URL = "https://api.dingtalk.com"
LEGACY_API_URL = "https://oapi.dingtalk.com"
_app_token_cache: dict[str, tuple[str, float]] = {}
_app_token_lock = asyncio.Lock()


class DingTalkAuthError(Exception):
    """上游登录、身份或企业成员校验失败；不要包含上游令牌。"""


@dataclass(frozen=True)
class DingTalkIdentity:
    corp_id: str
    union_id: str
    user_id: str


def configured(settings=None) -> bool:
    cfg = settings or get_settings()
    values = (cfg.dingtalk_client_id, cfg.dingtalk_client_secret,
              cfg.dingtalk_corp_id, cfg.dingtalk_redirect_uri)
    if not all(isinstance(value, str) and value.strip() for value in values):
        return False
    redirect = urlparse(cfg.dingtalk_redirect_uri)
    return bool(
        redirect.hostname
        and redirect.path == "/admin/user/dingtalk/callback"
        and not redirect.query and not redirect.fragment
        and (redirect.scheme == "https" or (
            not getattr(cfg, "is_prod", False)
            and redirect.scheme == "http"
            and redirect.hostname in {"localhost", "127.0.0.1"}
        ))
    )


def authorization_url(state: str, settings=None) -> str:
    cfg = settings or get_settings()
    if not configured(cfg):
        raise DingTalkAuthError("钉钉登录尚未配置")
    return AUTH_URL + "?" + urlencode({
        "client_id": cfg.dingtalk_client_id,
        "redirect_uri": cfg.dingtalk_redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
        "scope": "openid corpid Contact.User.Read",
    })


async def _json(response: httpx.Response) -> dict:
    try:
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DingTalkAuthError("钉钉接口暂不可用") from exc
    if not isinstance(body, dict):
        raise DingTalkAuthError("钉钉返回了无效响应")
    return body


async def _app_access_token(client: httpx.AsyncClient, cfg) -> str:
    """按应用缓存企业内部应用令牌，避免每位员工登录都刷新钉钉令牌。"""
    key = cfg.dingtalk_client_id
    async with _app_token_lock:
        cached = _app_token_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        body = await _json(await client.post(
            API_URL + "/v1.0/oauth2/accessToken",
            json={"appKey": cfg.dingtalk_client_id,
                  "appSecret": cfg.dingtalk_client_secret},
        ))
        token = body.get("accessToken")
        if not isinstance(token, str) or not token:
            raise DingTalkAuthError("未能校验企业成员身份")
        ttl = body.get("expireIn")
        if isinstance(ttl, int) and ttl > 120:
            _app_token_cache[key] = (token, time.monotonic() + ttl - 120)
        return token


async def resolve_identity(auth_code: str, settings=None) -> DingTalkIdentity:
    """只接受配置企业的内部成员；不把平台 Access Token 存入本地会话。"""
    cfg = settings or get_settings()
    if not configured(cfg) or not auth_code:
        raise DingTalkAuthError("钉钉登录尚未配置或授权码为空")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            token = await _json(await client.post(
                API_URL + "/v1.0/oauth2/userAccessToken",
                json={
                    "clientId": cfg.dingtalk_client_id,
                    "clientSecret": cfg.dingtalk_client_secret,
                    "code": auth_code,
                    "refreshToken": "",
                    "grantType": "authorization_code",
                },
            ))
            corp_id = token.get("corpId")
            user_token = token.get("accessToken")
            if corp_id != cfg.dingtalk_corp_id or not user_token:
                raise DingTalkAuthError("钉钉账号不属于已配置的企业")

            profile = await _json(await client.get(
                API_URL + "/v1.0/contact/users/me",
                headers={"x-acs-dingtalk-access-token": user_token},
            ))
            union_id = profile.get("unionId")
            if not isinstance(union_id, str) or not union_id:
                raise DingTalkAuthError("未能取得钉钉稳定用户标识")

            app_token = await _app_access_token(client, cfg)
            member = await _json(await client.post(
                LEGACY_API_URL + "/topapi/user/getbyunionid",
                params={"access_token": app_token},
                json={"unionid": union_id},
            ))
    except httpx.RequestError as exc:
        raise DingTalkAuthError("钉钉接口暂不可用") from exc

    result = member.get("result")
    if not isinstance(result, dict):
        raise DingTalkAuthError("钉钉未能确认企业内部成员身份")
    if (str(member.get("errcode")) != "0"
            or str(result.get("contact_type")) != "0"
            or not isinstance(result.get("userid"), str)
            or not result["userid"]):
        raise DingTalkAuthError("钉钉账号不是本企业内部成员")
    return DingTalkIdentity(corp_id=corp_id, union_id=union_id,
                            user_id=result["userid"])
