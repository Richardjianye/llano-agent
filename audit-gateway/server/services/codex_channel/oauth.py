"""Codex（ChatGPT）OAuth 登录与凭证换取

提取自 codex2api 的核心登录链路（admin/oauth.go + auth/token.go），只保留三件事：
发起授权（PKCE）→ 授权码换 token → refresh_token 续期。

登录必须由**人**在浏览器里完成授权，本模块只生成授权 URL 和兑换授权码，
不接触账号密码、扫码与验证码。

OpenAI OAuth 端点与 client_id 取自 Codex 官方 CLI 的公开客户端配置
（codex-rs/login/src/auth），redirect_uri 固定 localhost:1455 是官方 CLI 的
回调口径：授权后浏览器跳到这个地址（多半打不开），把地址栏整段 URL 或
code#state 粘回来即可兑换。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import httpx

# ==================== 端点常量（与 Codex CLI 一致） ====================

OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPES = "openid profile email offline_access"
# 刷新时不带 offline_access（与 codex2api auth/token.go 一致）
REFRESH_SCOPES = "openid profile email"

SESSION_TTL_SECONDS = 30 * 60


class OAuthError(RuntimeError):
    """登录/换票失败。message 可直接展示给管理员"""


class OAuthLoginRequired(OAuthError):
    """凭证已失效（refresh_token 被撤销/过期），需要人工重新登录"""


@dataclass
class TokenSet:
    """一次兑换/刷新拿到的完整凭证"""

    access_token: str
    refresh_token: str
    id_token: str = ""
    expires_at: float = 0.0  # access_token 过期时间（unix 秒）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenSet":
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            id_token=str(data.get("id_token") or ""),
            expires_at=float(data.get("expires_at") or 0),
        )


@dataclass
class AccountIdentity:
    """id_token 解析出的账号身份"""

    email: str = ""
    user_id: str = ""
    chatgpt_account_id: str = ""
    plan_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "user_id": self.user_id,
            "chatgpt_account_id": self.chatgpt_account_id,
            "plan_type": self.plan_type,
        }


# ==================== PKCE / state ====================


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def code_challenge_s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def build_auth_url(redirect_uri: str, state: str, verifier: str) -> str:
    """拼出授权 URL（PKCE S256），与 codex2api buildOAuthAuthorizeURL 一致"""
    from urllib.parse import urlencode

    params = urlencode(
        {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPES,
            "state": state,
            "code_challenge": code_challenge_s256(verifier),
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{params}"


# ==================== 内存会话（state/verifier 承接） ====================


@dataclass
class _PendingLogin:
    state: str
    code_verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)


class _SessionStore:
    """登录会话存储。进程内存即可：会话 30 分钟 TTL，量小且短命。

    进程重启丢会话没有关系——重新发起一次登录就行，凭证入库后不依赖这里。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, _PendingLogin] = {}

    def put(self, session_id: str, item: _PendingLogin) -> None:
        now = time.time()
        with self._lock:
            self._items = {
                k: v
                for k, v in self._items.items()
                if now - v.created_at < SESSION_TTL_SECONDS
            }
            self._items[session_id] = item

    def pop(self, session_id: str, state: Optional[str] = None) -> _PendingLogin:
        with self._lock:
            item = self._items.pop(session_id, None)
        if item is None or time.time() - item.created_at >= SESSION_TTL_SECONDS:
            raise OAuthError("登录会话不存在或已过期（有效期 30 分钟），请重新发起登录")
        if state is not None and state != item.state:
            raise OAuthError("state 不匹配，请重新发起登录")
        return item


_sessions = _SessionStore()


def start_login(redirect_uri: Optional[str] = None) -> Tuple[str, str]:
    """第一步：生成授权 URL。返回 (session_id, auth_url)"""
    redirect = (redirect_uri or DEFAULT_REDIRECT_URI).strip()
    session_id = secrets.token_urlsafe(16)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _sessions.put(session_id, _PendingLogin(state=state, code_verifier=verifier, redirect_uri=redirect))
    return session_id, build_auth_url(redirect, state, verifier)


def extract_code(raw: str) -> Tuple[str, str]:
    """从用户粘贴的内容里取 (code, state)

    兼容三种形态：
    - 整条回调 URL：``http://localhost:1455/auth/callback?code=xxx&state=yyy``
    - 裸 query 串：``code=xxx&state=yyy``
    - 纯 code：整段就是授权码（state 校验降级为不校验）
    """
    raw = (raw or "").strip().strip("\'\"")
    if not raw:
        raise OAuthError("授权码为空")

    if "=" in raw and not raw.startswith("http"):
        query = raw
    elif "://" in raw or "?" in raw:
        query = raw.split("?", 1)[1] if "?" in raw else ""
    else:
        return raw, ""

    from urllib.parse import parse_qs

    params = parse_qs(query)
    code = (params.get("code") or [""])[0].strip()
    state = (params.get("state") or [""])[0].strip()
    if not code:
        raise OAuthError("没有解析到授权码，请粘贴完整的回调 URL（含 code=...）")
    return code, state


# ==================== token 端点交互 ====================


async def _token_request(form: Dict[str, str], timeout: float = 30.0) -> Dict[str, Any]:
    """POST auth.openai.com/oauth/token，统一错误语义

    invalid_grant / invalid_client 一类是**永久失败**（凭证作废，重试无用），
    网络/5xx 是临时失败。上层据此决定是提示重新登录还是稍后再试。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                OAUTH_TOKEN_URL,
                data=form,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OAuthError(f"连接 auth.openai.com 失败：{exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        body = {}

    if not isinstance(body, dict):
        body = {}
    if resp.status_code != 200:
        err = str(body.get("error") or "")
        if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise OAuthLoginRequired(
                f"凭证已失效（{err}），需要重新登录获取授权"
            )
        raise OAuthError(f"token 端点返回 HTTP {resp.status_code}，请稍后重试")

    if not isinstance(body, dict) or not body.get("access_token"):
        raise OAuthError("token 端点响应异常（缺少 access_token）")
    return body


def parse_id_token(id_token: str) -> AccountIdentity:
    """解出 id_token payload 里的身份信息（不验签，仅读取声明）

    传输链路由 TLS 保证；此处与 codex2api parseIDToken 同口径。
    """
    identity = AccountIdentity()
    if not id_token or "." not in id_token:
        return identity
    try:
        seg = id_token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return identity

    if not isinstance(payload, dict):
        return identity
    raw_auth = payload.get("https://api.openai.com/auth")
    if raw_auth is not None and not isinstance(raw_auth, dict):
        return identity
    identity.email = str(payload.get("email") or "")
    identity.user_id = str(payload.get("https://api.openai.com/auth") and
                          payload["https://api.openai.com/auth"].get("chatgpt_user_id") or
                          payload.get("sub") or "")
    auth_claim = payload.get("https://api.openai.com/auth") or {}
    identity.chatgpt_account_id = str(auth_claim.get("chatgpt_account_id") or "")
    identity.plan_type = str(auth_claim.get("chatgpt_plan_type") or "")
    return identity


def _build_token_set(body: Dict[str, Any]) -> TokenSet:
    expires_in = int(body.get("expires_in") or 0)
    return TokenSet(
        access_token=str(body.get("access_token") or ""),
        refresh_token=str(body.get("refresh_token") or ""),
        id_token=str(body.get("id_token") or ""),
        expires_at=time.time() + expires_in if expires_in else 0.0,
    )


async def exchange_code_with(
    code_verifier: str, redirect_uri: str, code_or_url: str
) -> Tuple[TokenSet, AccountIdentity]:
    """授权码换 token（凭据在调用方手里，跨进程流程用这个）"""
    code, _ = extract_code(code_or_url)
    body = await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        }
    )
    tokens = _build_token_set(body)
    if not tokens.refresh_token:
        raise OAuthError("授权服务器未返回 refresh_token，请确认授权时勾选了 offline_access")
    return tokens, parse_id_token(tokens.id_token)


async def exchange_code(session_id: str, code_or_url: str) -> Tuple[TokenSet, AccountIdentity]:
    """第二步：授权码换 token。返回 (TokenSet, 账号身份)"""
    _, state = extract_code(code_or_url)
    sess = _sessions.pop(session_id, state or None)
    return await exchange_code_with(sess.code_verifier, sess.redirect_uri, code_or_url)


async def refresh_tokens(refresh_token: str) -> Tuple[TokenSet, AccountIdentity]:
    """refresh_token 续期。OpenAI 会轮换 refresh_token，必须把新的存回去"""
    body = await _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "scope": REFRESH_SCOPES,
        }
    )
    tokens = _build_token_set(body)
    # 个别响应不带新 refresh_token，沿用旧的
    if not tokens.refresh_token:
        tokens.refresh_token = refresh_token
    return tokens, parse_id_token(tokens.id_token)


async def start_gateway_login(owner_uid: str, redirect_uri: Optional[str] = None) -> Tuple[str, str]:
    """管理 API 的共享会话；CLI 的单进程会话仍由 start_login 处理。"""
    from urllib.parse import urlsplit
    from utils.cache import CacheManager
    redirect = (redirect_uri or DEFAULT_REDIRECT_URI).strip()
    try:
        parsed = urlsplit(redirect)
        valid = (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
                 and parsed.port is not None and parsed.path == "/auth/callback"
                 and not parsed.query and not parsed.fragment and not parsed.username)
    except ValueError:
        valid = False
    if not valid:
        raise OAuthError("回调地址须为本机 HTTP /auth/callback")
    session_id, state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    payload = json.dumps({"owner_uid": owner_uid, "state": state, "code_verifier": verifier, "redirect_uri": redirect})
    async with CacheManager.get_instance().redis_connection() as redis:
        if not await redis.set("codex:oauth:" + session_id, payload, ex=SESSION_TTL_SECONDS, nx=True):
            raise OAuthError("无法创建授权会话，请重试")
    return session_id, build_auth_url(redirect, state, verifier)


async def exchange_gateway_code(owner_uid: str, session_id: str, code_or_url: str) -> Tuple[TokenSet, AccountIdentity]:
    from utils.cache import CacheManager
    _, state = extract_code(code_or_url)
    if not state:
        raise OAuthError("请粘贴含 code 和 state 的完整回调 URL")
    key = "codex:oauth:" + session_id
    async with CacheManager.get_instance().redis_connection() as redis:
        raw = await redis.get(key)
        if not raw:
            raise OAuthError("登录会话不存在或已过期，请重新发起登录")
        pending = json.loads(raw)
        if pending["owner_uid"] != owner_uid:
            raise OAuthError("授权会话不属于当前管理员")
        if not secrets.compare_digest(state.encode(), pending["state"].encode()):
            raise OAuthError("state 不匹配，请重新发起登录")
        raw = await redis.getdel(key)
    if not raw:
        raise OAuthError("授权会话已使用，请重新发起登录")
    pending = json.loads(raw)
    return await exchange_code_with(pending["code_verifier"], pending["redirect_uri"], code_or_url)
