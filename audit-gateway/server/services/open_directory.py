"""对外成员搜索 —— 给 OSS 等第三方平台用（2026-10-19）

契约见 server/MEMBER_SEARCH_API.md 第 2 节。要点：

- **应用凭据，不是员工凭据**。接入应用登记在 sso_clients（总裁登记，见
  2026-10-14-sso-clients.sql），allowed_scopes 里带 `directory.read` 的机密应用
  （有 client_secret）才能用 client_credentials 换一个 10 分钟的不透明令牌。
- **只回第三方建权限表要用的东西**：uid（网关用户 ID，改名、改昵称、调部门都不变）、
  nickname（网关昵称）、status（离职/停用了它要能同步掉）。不回账号、角色、部门、钉钉 ID。
- **不能拿来导出全员**：关键字与 uids 至少给一个；一页最多 50 条；每个应用每分钟有上限。
- **每次调用都留痕**（open_api_call_logs），关键字只存摘要。

这里**不 import SSOClient ORM**：那张表的模型属于企业统一登录那批改动，两边各自合并
时不能撞同一个 __tablename__。只读它需要的五列，用一份独立的 MetaData。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Optional, Tuple

from sqlalchemy import Boolean, Column, MetaData, String, Table, select

SCOPE_DIRECTORY_READ = "directory.read"
TOKEN_TTL_SECONDS = 600
MAX_PAGE = 50
MAX_UIDS = 100
TOKEN_KEY_PREFIX = "open:access:"
RATE_KEY_PREFIX = "open:rate:"

_registry = MetaData()
# sso_clients 的只读视图（字段含义与写入方 router/sso.py 一致：secret_hash 是 sha256 hex）
sso_clients_view = Table(
    "sso_clients",
    _registry,
    Column("client_id", String(96), primary_key=True),
    Column("environment", String(16)),
    Column("client_type", String(16)),
    Column("allowed_scopes", String(128)),
    Column("secret_hash", String(64)),
    Column("enabled", Boolean),
)


class OpenApiError(Exception):
    """按 OAuth 2.0 的错误格式回给接入方"""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class OpenClient:
    client_id: str
    environment: str
    allowed_scopes: Tuple[str, ...]
    secret_hash: Optional[str]
    enabled: bool


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def verify_client(client: Optional[OpenClient], secret: str, *, environment: str) -> OpenClient:
    """校验应用凭据与 scope。任何一项不过都回同一句 invalid_client，不泄露是哪一项。"""
    if (
        client is None
        or not client.enabled
        or client.environment != environment
        or not client.secret_hash
        or not secret
        or not secrets.compare_digest(digest(secret), client.secret_hash)
    ):
        raise OpenApiError("invalid_client", "应用凭据无效", 401)
    if SCOPE_DIRECTORY_READ not in client.allowed_scopes:
        raise OpenApiError("unauthorized_client", "该应用未获准读取成员目录", 403)
    return client


async def load_client(client_id: str) -> Optional[OpenClient]:
    from db import init_async_session
    from loguru import logger
    from sqlalchemy.exc import DBAPIError

    if not client_id or len(client_id) > 96:
        return None
    maker = await init_async_session()
    try:
        async with maker() as session:
            row = (await session.execute(
                select(sso_clients_view).where(sso_clients_view.c.client_id == client_id)
            )).mappings().first()
    except DBAPIError as exc:
        # 统一登录那批迁移还没跑时表不存在：对外接口整体不可用，网关其他功能不受影响
        logger.warning(f"读取接入应用失败（sso_clients 是否已迁移？）: {exc.__class__.__name__}")
        return None
    if row is None:
        return None
    return OpenClient(
        client_id=row["client_id"],
        environment=row["environment"] or "",
        allowed_scopes=tuple((row["allowed_scopes"] or "").split()),
        secret_hash=row["secret_hash"],
        enabled=bool(row["enabled"]),
    )


def current_environment() -> str:
    from config import get_settings

    # 与 enterprise_sso.get_client 同一口径：prod 库里只认 prod 登记的应用
    return "prod" if get_settings().is_prod else "dev"


# ==================== 令牌（Redis，不透明） ====================


async def issue_token(client: OpenClient, redis) -> dict:
    token = secrets.token_urlsafe(32)
    payload = json.dumps({"client_id": client.client_id, "scope": SCOPE_DIRECTORY_READ})
    await redis.set(TOKEN_KEY_PREFIX + digest(token), payload, ex=TOKEN_TTL_SECONDS)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "scope": SCOPE_DIRECTORY_READ,
    }


async def resolve_token(token: str, redis) -> str:
    """返回 client_id；令牌无效或过期抛 invalid_token"""
    raw = await redis.get(TOKEN_KEY_PREFIX + digest(token)) if token else None
    if not raw:
        raise OpenApiError("invalid_token", "访问令牌无效或已过期", 401)
    data = json.loads(raw)
    if data.get("scope") != SCOPE_DIRECTORY_READ:
        raise OpenApiError("insufficient_scope", "令牌无权读取成员目录", 403)
    return data["client_id"]


async def check_rate(client_id: str, limit_per_minute: int, redis, minute: int) -> None:
    key = f"{RATE_KEY_PREFIX}{client_id}:{minute}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 120)
    if count > limit_per_minute:
        raise OpenApiError("rate_limited", f"调用过于频繁，每分钟最多 {limit_per_minute} 次", 429)


# ==================== 查询与结果 ====================


@dataclass(frozen=True)
class OpenMemberQuery:
    keyword: str = ""
    uids: Tuple[str, ...] = ()
    status: str = "active"
    limit: int = 20
    offset: int = 0

    def validated(self) -> "OpenMemberQuery":
        from dataclasses import replace

        keyword = (self.keyword or "").strip()[:64]
        uids = tuple(dict.fromkeys(u.strip() for u in self.uids if u and u.strip()))
        if not keyword and not uids:
            # 不给条件就是一页一页翻全员名册。要同步已知成员，用 uids 批量查
            raise OpenApiError("invalid_request", "keyword 与 uids 至少提供一个")
        if len(uids) > MAX_UIDS:
            raise OpenApiError("invalid_request", f"uids 最多 {MAX_UIDS} 个")
        if self.status not in ("active", "all"):
            raise OpenApiError("invalid_request", "status 可选值：active, all")
        return replace(
            self,
            keyword=keyword,
            uids=uids,
            limit=max(1, min(int(self.limit), MAX_PAGE)),
            offset=max(0, int(self.offset)),
        )

    def summary(self) -> str:
        """审计摘要：不含关键字原文"""
        parts = [f"keyword_len={len(self.keyword)}", f"uids={len(self.uids)}",
                 f"status={self.status}", f"limit={self.limit}", f"offset={self.offset}"]
        return " ".join(parts)

    def fingerprint(self) -> str:
        return digest(json.dumps([self.keyword, sorted(self.uids), self.status, self.limit, self.offset],
                                 ensure_ascii=False))


def open_member_payload(user) -> dict:
    """对外字段只有这三个。加字段前先想清楚第三方为什么需要它。"""
    return {
        "uid": user.uid,
        "nickname": user.nickname or user.realname or user.username,
        "status": "active" if user.status == 1 else "disabled",
    }


async def search_open_members(query: OpenMemberQuery) -> dict:
    from db import init_async_session
    from services.member_search import (STATUS_ACTIVE, STATUS_ALL, MemberQuery,
                                        build_count_query, build_search_query)

    member_query = MemberQuery(
        keyword=query.keyword,
        uids=query.uids,
        status=STATUS_ALL if query.status == "all" else STATUS_ACTIVE,
        limit=query.limit,
        offset=query.offset,
    )
    maker = await init_async_session()
    async with maker() as session:
        users = list((await session.execute(build_search_query(member_query))).scalars().all())
        total = int((await session.execute(build_count_query(member_query))).scalar_one())
    items = [open_member_payload(u) for u in users if u.uid]
    return {
        "items": items,
        "total": total,
        "limit": query.limit,
        "offset": query.offset,
        "has_more": query.offset + len(items) < total,
    }


async def record_call(*, client_id: str, endpoint: str, status: str,
                      query: Optional[OpenMemberQuery] = None, result_count: int = 0,
                      client_ip: Optional[str] = None, strict: bool = False) -> None:
    """写调用记录。strict=True 用于成功返回数据的那一次：记不下来就不给数据
    （成员名单出去了收不回来，留痕必须先于交付）。拒绝类记录写失败只留日志。"""
    from db import init_async_session
    from loguru import logger
    from model.database import OpenApiCallLog

    try:
        maker = await init_async_session()
        async with maker() as session:
            session.add(OpenApiCallLog(
                client_id=client_id[:96] or "-",
                endpoint=endpoint,
                status=status,
                query_digest=query.fingerprint() if query else None,
                query_summary=query.summary() if query else None,
                result_count=result_count,
                client_ip=(client_ip or "")[:64] or None,
            ))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"对外接口审计写入失败 client={client_id} endpoint={endpoint}: {exc}")
        if strict:
            raise OpenApiError("temporarily_unavailable", "暂时无法处理请求，请稍后重试", 503) from exc
