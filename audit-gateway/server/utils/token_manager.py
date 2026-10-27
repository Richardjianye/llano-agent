"""Token状态管理器 - 实现有状态JWT，支持黑/白名单"""

import json
import time
from typing import Any, Dict, Optional

from config import get_settings
from jose import jwt
from jose.exceptions import JWTError
from loguru import logger
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from utils.cache import CacheManager

config = get_settings()

# Redis 读不到时，「令牌是否被撤销」这个问题**没有答案** —— 而没有答案不等于
# 「被撤销了」。这两件事以前是一件事，代价是：网关机器上的 Redis 抖一下，
# 全体在线用户被顶下线，客户端还告诉他们「你已被管理员下线」。
#
# 负责人 2026-09-01 定：**全线登出不可接受**，撤销是极少用的功能，不能让它
# 影响正常使用。
#
# 放行的代价被两件事兜着，所以远比听起来小：
# 1. **禁用账号走的是 MySQL，不是 Redis**（refresh 里的 `user.status != 1`）。
#    离职、停用这类真正要紧的场景不受影响
# 2. access_token 本身有短过期时间，放行的窗口上限就是它
#
# 于是 Redis 故障期间丢掉的只有「管理员刚刚手工撤销的那一枚令牌」——
# 罕见 × 罕见。


class TokenStateManager:
    """管理JWT状态，实现黑名单和白名单机制"""

    def __init__(self):
        self.cache = CacheManager.get_instance()
        self.ACTIVE_TOKEN_PREFIX = "active_token:"
        self.USER_TOKENS_PREFIX = "user_tokens:"
        self.REVOKED_TOKEN_PREFIX = "revoked_token:"

    def _decode_token(self, token: str) -> Optional[Dict]:
        """尝试用 access/refresh 密钥解码 token"""
        payload = None
        for key in [config.jwt_secret_key, config.jwt_refresh_secret_key]:
            try:
                payload = jwt.decode(token, key, algorithms=config.algorithm)
                break
            except JWTError:
                continue
        return payload

    async def store_token(
        self, token: str, user_id: str, device_info: Optional[Dict[str, Any]] = None
    ) -> bool:
        """存储token到白名单（活跃token）"""
        try:
            payload = self._decode_token(token)
            if not payload:
                return False

            jti = payload.get("jti", token)
            exp = payload.get("exp", 0)
            uid = payload.get("sub", user_id)

            current_time = int(time.time())
            if exp <= current_time:
                logger.warning(f"尝试存储已过期的token: {jti}")
                return False

            ttl = exp - current_time
            token_data = {
                "uid": uid,
                "user_id": user_id,
                "exp": exp,
                "device": device_info or {},
                "created_at": current_time,
            }

            async with self.cache.redis_connection() as redis:
                await redis.set(
                    f"{self.ACTIVE_TOKEN_PREFIX}{jti}",
                    json.dumps(token_data),
                    ex=ttl,
                )
                await redis.sadd(f"{self.USER_TOKENS_PREFIX}{uid}", jti)
                await redis.expire(
                    f"{self.USER_TOKENS_PREFIX}{uid}",
                    config.refresh_token_expire_minutes * 60,
                )

            logger.info(f"Token已存储到白名单: jti={jti}, uid={uid}")
            return True

        except Exception as e:
            logger.error(f"存储token到白名单失败: {e}")
            return False

    async def revoke_token(
        self, token: str, reason: str = "logout", extra: Optional[Dict[str, Any]] = None
    ) -> bool:
        """撤销token（加入黑名单）。`extra` 并进撤销记录，刷新轮换用它记下继任者。"""
        try:
            payload = self._decode_token(token)
            if not payload:
                return False

            jti = payload.get("jti", token)
            uid = payload.get("sub")
            exp = payload.get("exp", 0)

            current_time = int(time.time())
            if exp <= current_time:
                return True

            ttl = exp - current_time

            async with self.cache.redis_connection() as redis:
                await redis.delete(f"{self.ACTIVE_TOKEN_PREFIX}{jti}")
                if uid:
                    await redis.srem(f"{self.USER_TOKENS_PREFIX}{uid}", jti)

                revoked_data = {
                    "reason": reason,
                    "revoked_at": current_time,
                    "original_exp": exp,
                    "uid": uid,
                    **(extra or {}),
                }
                await redis.set(
                    f"{self.REVOKED_TOKEN_PREFIX}{jti}",
                    json.dumps(revoked_data),
                    ex=ttl,
                )

            logger.info(f"Token已撤销: jti={jti}, reason={reason}")
            return True

        except Exception as e:
            logger.error(f"撤销token失败: {e}")
            return False

    async def is_token_revoked(self, token: str) -> bool:
        """检查token是否被撤销"""
        try:
            payload = self._decode_token(token)
            if not payload:
                return True

            jti = payload.get("jti", token)

            async with self.cache.redis_connection() as redis:
                revoked = await redis.get(f"{self.REVOKED_TOKEN_PREFIX}{jti}")
                if revoked:
                    return True

                active = await redis.get(f"{self.ACTIVE_TOKEN_PREFIX}{jti}")
                if not active:
                    return True

                return False

        except (RedisConnectionError, RedisTimeoutError, OSError, TimeoutError) as e:
            # 基础设施没答上来 —— 见文件头的说明，这里**放行**。
            # 单独一条日志级别拉到 error：它是「安全检查这次没做」，
            # 混在通用异常里会看不见。
            logger.error(f"撤销检查失败（Redis 不可用，本次放行）: {e}")
            return False

        except RedisError as e:
            # Redis 应答了，但是我们不认识的错误。它至少证明服务是活的，
            # 所以按可疑处理 —— 与上面那格的区别是「答了」和「没答」。
            logger.error(f"撤销检查异常（Redis 有应答，按已撤销处理）: {e}")
            return True

        except Exception as e:
            logger.error(f"检查token状态失败: {e}")
            return True

    # ------------------------------------------------------------------
    # 刷新令牌轮换的宽限重放（2026-09-29）
    #
    # refresh_token 一次性：网关收下旧的那一刻就作废它、签发新的一对。如果这次
    # 的**回包**在路上丢了（休眠唤醒时网络半通、客户端 15 秒超时而网关其实处理完了），
    # 客户端手里只剩那枚已作废的旧令牌，下一轮刷新 10003，被踢回登录页。
    #
    # 判据是「继任者有没有被用过」，而不是时间窗：丢包之后电脑往往接着睡几个小时，
    # 固定的几分钟宽限救不回来。继任者还原封不动 = 客户端根本没收到它，允许旧令牌
    # 再换一次，同时作废那个没人拿到的继任者；继任者已经被用过（它自己也轮换了）
    # = 真正的重放，照旧拒绝。
    #
    # 代价：偷到旧令牌的人，只能在合法客户端还没用上继任者之前抢先换一次，而一旦
    # 抢到，合法客户端下次刷新就会被挤下线 —— 和没有宽限时「谁先用谁赢」是同一个
    # 暴露面，只是多了一个可见的信号。设备 API Key 才是长期凭据，这对令牌只是
    # 「账号还在不在」的心跳，不值得为它让员工半夜被登出。
    # ------------------------------------------------------------------

    def token_identity(self, token: str) -> Optional[Dict[str, Any]]:
        """令牌的 jti / exp，供撤销记录里指认继任者。"""
        payload = self._decode_token(token)
        if not payload:
            return None
        return {"jti": payload.get("jti", token), "exp": payload.get("exp", 0)}

    async def get_revocation(self, token: str) -> Optional[Dict[str, Any]]:
        """这枚令牌的撤销记录；没有记录或读不到都返回 None。"""
        try:
            payload = self._decode_token(token)
            if not payload:
                return None
            jti = payload.get("jti", token)
            async with self.cache.redis_connection() as redis:
                raw = await redis.get(f"{self.REVOKED_TOKEN_PREFIX}{jti}")
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"读取撤销记录失败: {e}")
            return None

    async def rotation_successor_unused(self, record: Optional[Dict[str, Any]]) -> bool:
        """撤销记录是一次刷新轮换，且它签发的 refresh 继任者还原封未动。"""
        if not record or record.get("reason") != "refresh":
            return False
        successor = record.get("successor_refresh_jti")
        if not successor:
            return False
        try:
            async with self.cache.redis_connection() as redis:
                if await redis.get(f"{self.REVOKED_TOKEN_PREFIX}{successor}"):
                    return False
                return bool(await redis.get(f"{self.ACTIVE_TOKEN_PREFIX}{successor}"))
        except Exception as e:
            logger.error(f"检查刷新继任者失败: {e}")
            return False

    async def revoke_jti(self, jti: str, uid: Optional[str], exp: int, reason: str) -> None:
        """只知道 jti 时作废一枚令牌（轮换宽限里那个没人收到的继任者）。"""
        ttl = int(exp or 0) - int(time.time())
        if not jti or ttl <= 0:
            return
        try:
            async with self.cache.redis_connection() as redis:
                await redis.delete(f"{self.ACTIVE_TOKEN_PREFIX}{jti}")
                if uid:
                    await redis.srem(f"{self.USER_TOKENS_PREFIX}{uid}", jti)
                await redis.set(
                    f"{self.REVOKED_TOKEN_PREFIX}{jti}",
                    json.dumps({"reason": reason, "revoked_at": int(time.time()), "original_exp": exp, "uid": uid}),
                    ex=ttl,
                )
        except Exception as e:
            logger.error(f"作废继任令牌失败: {e}")

    async def revoke_user_tokens(self, uid: str, reason: str = "security") -> int:
        """撤销用户的所有活跃token"""
        # OIDC 应用访问令牌与工作台 JWT 分库存放，但账号停用/改密必须一并失效。
        # Redis 不可用时 UserInfo 会因读不到活跃令牌而拒绝访问。
        try:
            from services.enterprise_sso import revoke_user_sessions
            await revoke_user_sessions(uid)
        except Exception as e:
            logger.error(f"撤销统一登录应用会话失败: uid={uid}, error={e}")
        try:
            count = 0
            async with self.cache.redis_connection() as redis:
                jti_list = await redis.smembers(f"{self.USER_TOKENS_PREFIX}{uid}")

                if not jti_list:
                    return 0

                for jti in jti_list:
                    jti_str = jti if isinstance(
                        jti, str) else jti.decode("utf-8")
                    token_data = await redis.get(f"{self.ACTIVE_TOKEN_PREFIX}{jti_str}")

                    if token_data:
                        data = json.loads(token_data)
                        exp = data.get("exp", 0)
                        ttl = max(0, exp - int(time.time()))
                        revoked_data = {
                            "reason": reason,
                            "revoked_at": int(time.time()),
                            "original_exp": exp,
                            "uid": uid,
                        }
                        await redis.set(
                            f"{self.REVOKED_TOKEN_PREFIX}{jti_str}",
                            json.dumps(revoked_data),
                            ex=ttl,
                        )

                    await redis.delete(f"{self.ACTIVE_TOKEN_PREFIX}{jti_str}")
                    count += 1

                await redis.delete(f"{self.USER_TOKENS_PREFIX}{uid}")

            logger.info(f"已撤销用户的所有token: uid={uid}, count={count}")
            return count

        except Exception as e:
            logger.error(f"撤销用户所有token失败: {e}")
            return 0

    async def cleanup_user_tokens(
        self, uid: str, except_jti: Optional[str] = None
    ) -> int:
        """清理用户的无效token"""
        try:
            cleaned_count = 0
            current_time = int(time.time())

            async with self.cache.redis_connection() as redis:
                jti_list = await redis.smembers(f"{self.USER_TOKENS_PREFIX}{uid}")

                if not jti_list:
                    return 0

                for jti in jti_list:
                    jti_str = jti if isinstance(
                        jti, str) else jti.decode("utf-8")

                    if except_jti and jti_str == except_jti:
                        continue

                    token_data = await redis.get(f"{self.ACTIVE_TOKEN_PREFIX}{jti_str}")

                    if not token_data:
                        await redis.srem(f"{self.USER_TOKENS_PREFIX}{uid}", jti_str)
                        cleaned_count += 1
                        continue

                    data = json.loads(token_data)
                    exp = data.get("exp", 0)
                    if exp <= current_time:
                        await redis.srem(f"{self.USER_TOKENS_PREFIX}{uid}", jti_str)
                        cleaned_count += 1

            if cleaned_count > 0:
                logger.info(f"清理用户无效token: uid={uid}, count={cleaned_count}")

            return cleaned_count

        except Exception as e:
            logger.error(f"清理用户无效token失败: {e}")
            return 0


# ========== 全局单例 ==========

_token_manager_instance = None


def get_token_manager() -> TokenStateManager:
    global _token_manager_instance
    if _token_manager_instance is None:
        _token_manager_instance = TokenStateManager()
    return _token_manager_instance
