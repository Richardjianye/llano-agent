"""JWT 认证 & 本系统密码哈希

本模块只处理**不可逆**的本系统登录密码（bcrypt）。
需要还原明文的外部凭证（中台账号、上游 API Key）一律走 utils/secret_box.py，
两者密钥与实现严格分离——见 TODO 决策 #5。
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from config import get_settings
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from loguru import logger
from model.database import User

config = get_settings()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/user/login")

if config.jwt_secret_key == "secret" or config.jwt_refresh_secret_key == "secret":
    logger.warning(
        "即将使用默认JWT密钥，请及时前往 .env 文件增改 JWT_SECRET_KEY 及 JWT_REFRESH_SECRET_KEY 字段"
    )


# ========== 密码哈希 ==========

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


# ---- 异步包装：请求路径上一律用这两个，不要直接调上面的同步版 ----
#
# bcrypt 是**故意慢**的，而且慢在纯 CPU 上。2026-09-07 在生产机
# （Intel Xeon Platinum 8255C @ 2.5GHz）上实测：cost=12 时一次校验 **299ms**。
#
# 在 `async def` 里直接调同步版，这 299ms 里整个 worker 的事件循环是停死的 ——
# 不只是这个登录请求慢，**同一个 worker 上所有正在流式输出的对话一起卡住**。
# 生产是 WEB_CONCURRENCY=2，35 人在上班时集中登录就是
# 35 × 0.299 ÷ 2 ≈ 5 秒的全面停顿，而日志里每条请求看着都正常，
# 只是延迟莫名其妙 —— 这类故障从现象反推原因非常费劲。
#
# 丢进线程池即可：bcrypt 在 hash 期间会释放 GIL，所以这里是真并行，
# 不是把阻塞从一个地方挪到另一个地方。
# 同一手法见 services/usage.py 里附件落盘的 asyncio.to_thread。
#
# 仍然直接用同步版的两处，都不在请求路径上，是对的：
#   - db/__init__.py 的 _seed_president（启动时单进程跑，还没开始服务）
#   - scripts/reset_account_password.py（命令行工具）

async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def check_password_async(password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(check_password, password, hashed_password)


# ========== JWT Token ==========

class JWTAuth:
    def create_token(
        self,
        subject: User,
        expires_delta: Optional[int],
        token_type: str = "access",
        password_change_required: bool = False,
        auth_time: Optional[int] = None,
    ) -> str:
        """
        通用 JWT token 创建

        :param subject: token 主体（User 对象）
        :param expires_delta: 过期时间（分钟）
        :param token_type: "access" 或 "refresh"
        """
        if expires_delta is not None:
            expires_delta = datetime.now(
                timezone.utc) + timedelta(minutes=expires_delta)
        else:
            if token_type == "access":
                expires_delta = datetime.now(timezone.utc) + timedelta(
                    minutes=config.access_token_expire_minutes
                )
            else:
                expires_delta = datetime.now(timezone.utc) + timedelta(
                    minutes=config.refresh_token_expire_minutes
                )

        jti = str(uuid.uuid4())

        to_encode = {
            "exp": expires_delta,
            "sub": subject.uid,
            "nic": subject.nickname,
            "per": subject.permission,
            "jti": jti,
            # 真实登录时间在 refresh 轮换时沿用，供 OIDC auth_time 使用。
            "auth_time": int(auth_time or datetime.now(timezone.utc).timestamp()),
        }

        if password_change_required:
            to_encode["password_change_required"] = True

        secret_key = (
            config.jwt_secret_key if token_type == "access" else config.jwt_refresh_secret_key
        )
        return jwt.encode(to_encode, secret_key, config.algorithm)

    def create_access_token(self, subject: User, expires_delta=None, password_change_required=False,
                            auth_time: Optional[int] = None) -> str:
        return self.create_token(subject, expires_delta, "access", password_change_required, auth_time)

    def create_refresh_token(self, subject: User, expires_delta=None, password_change_required=False,
                             auth_time: Optional[int] = None) -> str:
        return self.create_token(subject, expires_delta, "refresh", password_change_required, auth_time)
