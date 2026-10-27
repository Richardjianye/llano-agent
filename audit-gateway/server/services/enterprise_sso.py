"""OIDC 接入应用、一次性授权码及按应用隔离的短期访问令牌。"""

import base64
import hashlib
import json
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from jose import jwt
from sqlalchemy import select

from config import get_settings
from db import init_async_session
from model.database import SSOClient
from utils.cache import CacheManager

CODE_TTL = 60
TOKEN_TTL = 300
SCOPES = frozenset({"openid", "profile", "org"})


class SSOError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request"):
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


@lru_cache(maxsize=4)
def _signing_material(value: str):
    """配置在进程内固定；密钥轮换时重启进程以整体切换 kid 与私钥。"""
    try:
        if value.startswith("/"):
            with open(value, "rb") as file:
                pem = file.read()
        else:
            pem = value.replace("\\n", "\n").encode()
    except OSError as exc:
        raise SSOError("统一登录签名密钥不可读", 503, "temporarily_unavailable") from exc
    try:
        key = serialization.load_pem_private_key(pem, password=None)
        public = key.public_key().public_numbers()
    except (ValueError, TypeError, AttributeError) as exc:
        raise SSOError("统一登录签名密钥无效", 503, "temporarily_unavailable") from exc
    if not hasattr(public, "n") or not hasattr(public, "e"):
        raise SSOError("统一登录仅支持 RSA 签名密钥", 503, "temporarily_unavailable")
    if key.key_size < 2048:
        raise SSOError("统一登录 RSA 签名密钥至少需要 2048 位", 503, "temporarily_unavailable")
    return pem, public


def issuer_settings():
    cfg = get_settings()
    issuer = (cfg.sso_issuer or "").rstrip("/")
    try:
        parsed = urlparse(issuer)
        parsed.port  # 访问属性以拒绝非法端口格式
        valid = (parsed.scheme == "https" or (
            not cfg.is_prod and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        )) and bool(parsed.hostname) and not parsed.username and not parsed.password \
            and not parsed.path and not parsed.query and not parsed.fragment
    except ValueError:
        valid = False
    if (not valid or not cfg.sso_org_id or not cfg.sso_signing_private_key
            or not cfg.sso_signing_key_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cfg.sso_signing_key_id)):
        raise SSOError("统一登录发admin尚未配置", 503, "temporarily_unavailable")
    pem, public = _signing_material(cfg.sso_signing_private_key)
    return cfg, issuer, pem, public


def _b64int(value: int) -> str:
    data = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _jwk(public, kid: str) -> dict:
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
            "n": _b64int(public.n), "e": _b64int(public.e)}


def jwks() -> list[dict]:
    cfg, _, _, public = issuer_settings()
    keys = [_jwk(public, cfg.sso_signing_key_id)]
    archive = getattr(cfg, "sso_public_key_archive_dir", None)
    if archive:
        root = Path(archive)
        if not root.is_dir():
            raise SSOError("旧公钥归档目录不可读", 503, "temporarily_unavailable")
        for path in sorted(root.glob("*.pem")):
            kid = path.stem
            if kid == cfg.sso_signing_key_id:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kid):
                raise SSOError("旧公钥编号无效", 503, "temporarily_unavailable")
            try:
                old_key = serialization.load_pem_public_key(path.read_bytes())
                if old_key.key_size < 2048:
                    raise ValueError("short RSA key")
                numbers = old_key.public_numbers()
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise SSOError("旧公钥归档无效", 503, "temporarily_unavailable") from exc
            if not hasattr(numbers, "n") or not hasattr(numbers, "e"):
                raise SSOError("旧公钥必须为 RSA", 503, "temporarily_unavailable")
            keys.append(_jwk(numbers, kid))
    return keys


def valid_redirect(uri: str, *, production: bool) -> bool:
    try:
        parsed = urlparse(uri)
        parsed.port
        return (bool(parsed.hostname) and bool(parsed.path) and not parsed.fragment
                and not parsed.username and not parsed.password
                and (parsed.scheme == "https" or (
                    not production and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1"}
                )))
    except ValueError:
        return False


async def get_client(client_id: str) -> SSOClient:
    maker = await init_async_session()
    async with maker() as session:
        client = await session.get(SSOClient, client_id)
    expected_environment = "prod" if get_settings().is_prod else "dev"
    if client is None or not client.enabled or client.environment != expected_environment:
        raise SSOError("接入应用不存在或已停用", 400, "invalid_client")
    return client


def check_redirect(client: SSOClient, redirect_uri: str) -> None:
    cfg = get_settings()
    if not valid_redirect(redirect_uri, production=cfg.is_prod) or redirect_uri not in json.loads(client.redirect_uris):
        raise SSOError("回调地址未登记")


def check_scope(client: SSOClient, scope: str) -> list[str]:
    requested = scope.split()
    if (not requested or "openid" not in requested or len(requested) != len(set(requested))
            or not set(requested) <= SCOPES.intersection(client.allowed_scopes.split())):
        raise SSOError("请求的 scope 未获授权", 400, "invalid_scope")
    return requested


def check_client(client: SSOClient, redirect_uri: str, scope: str) -> list[str]:
    check_redirect(client, redirect_uri)
    return check_scope(client, scope)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


async def issue_code(*, client: SSOClient, redirect_uri: str, scope: str, nonce: str,
                     challenge: str, uid: str, auth_time: int, session_jti: str) -> str:
    code = secrets.token_urlsafe(32)
    data = {"client_id": client.client_id, "redirect_uri": redirect_uri,
            "scope": scope, "nonce": nonce, "challenge": challenge,
            "uid": uid, "auth_time": auth_time, "session_jti": session_jti}
    async with CacheManager.get_instance().redis_connection() as redis:
        data["epoch"] = int(await redis.get("sso:epoch:" + uid) or 0)
        saved = await redis.set("sso:code:" + _digest(code), json.dumps(data), ex=CODE_TTL, nx=True)
    if not saved:
        raise SSOError("无法签发授权码", 503, "temporarily_unavailable")
    return code


async def redeem_code(code: str, verifier: str, client: SSOClient, redirect_uri: str) -> dict:
    # 消费发生在校验前：错误尝试也会烧掉授权码，避免并发重放。
    async with CacheManager.get_instance().redis_connection() as redis:
        raw = await redis.getdel("sso:code:" + _digest(code))
    if not raw:
        raise SSOError("授权码不存在或已使用", 400, "invalid_grant")
    data = json.loads(raw)
    if (data["client_id"] != client.client_id or data["redirect_uri"] != redirect_uri
            or not secrets.compare_digest(challenge_of(verifier), data["challenge"])):
        raise SSOError("授权码与客户端、回调或 PKCE 不匹配", 400, "invalid_grant")
    async with CacheManager.get_instance().redis_connection() as redis:
        active = await redis.get("active_token:" + data["session_jti"])
        epoch = int(await redis.get("sso:epoch:" + data["uid"]) or 0)
    if not active or epoch != data["epoch"]:
        raise SSOError("发起授权的登录会话已失效", 400, "invalid_grant")
    return data


async def issue_tokens(data: dict, user, dept_code: str | None) -> dict:
    cfg, issuer, pem, _ = issuer_settings()
    if not user.uid or user.status != 1 or user.account_kind != "internal":
        raise SSOError("账号不可用", 403, "access_denied")
    now = int(time.time())
    claims = {"iss": issuer, "sub": user.uid, "aud": data["client_id"],
              "exp": now + TOKEN_TTL, "iat": now, "auth_time": data["auth_time"],
              "nonce": data["nonce"], "account_status": "active"}
    if "org" in data["scope"].split():
        claims.update({"org_id": cfg.sso_org_id, "dept_code": dept_code})
    id_token = jwt.encode(claims, pem, algorithm="RS256", headers={"kid": cfg.sso_signing_key_id})
    access = secrets.token_urlsafe(32)
    key = "sso:access:" + _digest(access)
    async with CacheManager.get_instance().redis_connection() as redis:
        saved = await redis.set(key, json.dumps({"uid": user.uid, "client_id": data["client_id"],
                                                 "scope": data["scope"], "epoch": data["epoch"]}),
                                ex=TOKEN_TTL, nx=True)
        if not saved:
            raise SSOError("无法签发应用访问令牌", 503, "temporarily_unavailable")
        await redis.sadd("sso:user:" + user.uid, key)
        await redis.expire("sso:user:" + user.uid, TOKEN_TTL)
        if int(await redis.get("sso:epoch:" + user.uid) or 0) != data["epoch"]:
            await redis.delete(key)
            raise SSOError("登录会话已失效", 400, "invalid_grant")
    return {"access_token": access, "id_token": id_token, "token_type": "Bearer",
            "expires_in": TOKEN_TTL, "scope": data["scope"]}


async def access_record(token: str) -> dict:
    async with CacheManager.get_instance().redis_connection() as redis:
        raw = await redis.get("sso:access:" + _digest(token))
    if not raw:
        raise SSOError("应用访问令牌无效", 401, "invalid_token")
    record = json.loads(raw)
    async with CacheManager.get_instance().redis_connection() as redis:
        epoch = int(await redis.get("sso:epoch:" + record["uid"]) or 0)
    if epoch != record["epoch"]:
        raise SSOError("应用访问令牌已撤销", 401, "invalid_token")
    return record


async def revoke_access(token: str) -> None:
    async with CacheManager.get_instance().redis_connection() as redis:
        await redis.delete("sso:access:" + _digest(token))


async def revoke_user_sessions(uid: str) -> None:
    """账号停用、改密或全局退出时，使该用户所有应用访问令牌失效。"""
    async with CacheManager.get_instance().redis_connection() as redis:
        index = "sso:user:" + uid
        await redis.incr("sso:epoch:" + uid)
        keys = await redis.smembers(index)
        if keys:
            await redis.delete(*keys)
        await redis.delete(index)
