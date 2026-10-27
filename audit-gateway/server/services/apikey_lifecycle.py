"""API Key 的过期与闲置失效（2026-10-19）

## 之前

Key 只有两种死法：客户端退出时自己吊销（/v1/device/revoke），或管理员手动吊销 /
停用账号。客户端每次登录都签一把新 Key（product-auth.provisionProductSession），
重装、换机、清数据、登录态过期再登 —— 旧 Key 全都留在库里永久有效。生产 2026-10-19：
233 把有效个人 Key，44 把超过 7 天没用过。

## 闲置失效，以及它为什么不会踢掉正在用的客户端

规则：个人 Key（scope=private）最后一次使用（从没用过则按签发时间）距今超过
`effective_idle_days` 天（默认 30），即**正式吊销**（reason=idle_30d）：
- 认证时碰到，当场拒绝并吊销
- 后台巡检（start_idle_sweeper，默认每小时）把其余闲置 Key 一并吊销，
  所以台账里看到的「有效」就是真的有效，不用等它下一次被拿来用

不会误伤在用客户端，依据是客户端的续期流程（apps/desktop/electron/product-auth.ts）：

1. 客户端只持有一对 JWT + 一把设备 Key，Key 本身从不轮换
2. 每次 `ProductSessionCoordinator.validate` 都是先按需刷新 JWT，再拿设备 Key
   调 `/v1/models` —— 也就是**每次刷新 refresh token，设备 Key 都被用了一次**，
   last_used_at 跟着更新
3. refresh token 有效期 `refresh_token_expire_minutes`（7 天），每次刷新顺延

所以一把 Key 闲置超过 7 天，对应客户端的 refresh token 必然已经过期：那台客户端
无论如何都得重新登录（并签一把新 Key）。把闲置阈值钉在「refresh 有效期 + 1 天」
以上，闲置失效就只会清掉**已经没有客户端能用**的 Key。

2026-09 的夜间掉线事故是客户端把刚轮换的 refresh token 弄丢了（见 main.ts
validateStoredProductSession 的注释），与服务端有效期无关；这里的设计保证服务端
不会成为第二个原因。默认 30 天，是 7 天的四倍余量，覆盖 touch 偶发写失败。

## 为什么不在签发时给设备 Key 设固定有效期

固定到期会让一台**一直在用**的客户端在第 N 天准时掉线：客户端没有续 Key 的流程，
只会在 401 后清掉登录态，让人重新登录。要做绝对有效期，得先给客户端加
「临期轮换设备 Key」（服务端发新 Key、旧 Key 留短暂宽限），那是一次客户端发版。
管理员手动签的 Key 仍可在签发时填有效期（valid_days / expires_at），不受影响。

群聊会话令牌（scope=group）本来就有 8 小时的绝对有效期，不参与闲置判定。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

STATE_ACTIVE = "active"
STATE_REVOKED = "revoked"
STATE_EXPIRED = "expired"
STATE_IDLE = "idle"
STATES = (STATE_ACTIVE, STATE_REVOKED, STATE_EXPIRED, STATE_IDLE)

REASON_CLIENT_LOGOUT = "client_logout"
REASON_ADMIN = "admin"
REASON_ADMIN_BATCH = "admin_batch"
# 闲置吊销的原因写成 idle_<天数>d（如 idle_30d），一眼看出是按多少天的阈值清掉的；
# revoked_by 区分自动巡检（system）与管理员手动清理（其 uid）。
REASON_IDLE_PREFIX = "idle_"
SYSTEM_ACTOR = "system"
REASON_ACCOUNT_DISABLED = "account_disabled"
REASON_PASSWORD_RESET = "password_reset"
REASON_SELF_PASSWORD_CHANGE = "self_password_change"

IDLE_MESSAGE = "API Key 长期未使用已失效"


def idle_reason(idle_days: int) -> str:
    return f"{REASON_IDLE_PREFIX}{int(idle_days)}d"


def effective_idle_days(configured: int, refresh_token_minutes: Optional[int]) -> int:
    """实际生效的闲置阈值（天）。0 表示关闭。

    配得再小也不低于 refresh token 有效期 + 1 天：低于它就会清掉 refresh 还活着的
    客户端的 Key —— 那正是「正常在用却被踢回登录页」。
    """
    if not configured or configured <= 0:
        return 0
    refresh_days = math.ceil((refresh_token_minutes or 0) / (60 * 24))
    return max(int(configured), refresh_days + 1)


def last_activity(record) -> Optional[datetime]:
    return getattr(record, "last_used_at", None) or getattr(record, "created_at", None)


def participates_in_idle(record) -> bool:
    return (getattr(record, "scope", None) or "private") == "private"


def is_idle(record, current: datetime, idle_days: int) -> bool:
    if idle_days <= 0 or not participates_in_idle(record):
        return False
    seen = last_activity(record)
    if seen is None:
        return False
    return seen < current - timedelta(days=idle_days)


def idle_deadline(record, idle_days: int) -> Optional[datetime]:
    """按现在的使用情况，这把 Key 哪天会闲置失效；不参与闲置判定时为 None"""
    if idle_days <= 0 or not participates_in_idle(record):
        return None
    seen = last_activity(record)
    return seen + timedelta(days=idle_days) if seen else None


def key_state(record, current: datetime, idle_days: int) -> str:
    """吊销 > 过期 > 闲置 > 有效。与 utils/gateway_auth 的拒绝顺序一致。"""
    if record.status != 1:
        return STATE_REVOKED
    if record.expires_at is not None and record.expires_at < current:
        return STATE_EXPIRED
    if is_idle(record, current, idle_days):
        return STATE_IDLE
    return STATE_ACTIVE


def configured_idle_days() -> int:
    from config import get_settings

    settings = get_settings()
    return effective_idle_days(settings.api_key_idle_days, settings.refresh_token_expire_minutes)


# ==================== 自动巡检 ====================

_sweep_task = None
SWEEP_LOCK_KEY = "apikey:idle_sweep:lock"


async def sweep_idle_keys(current: Optional[datetime] = None) -> int:
    """吊销全部闲置的个人 Key，返回本次吊销数。幂等：只动 status=1 的行。"""
    from db.selects.apikey import idle_key_ids
    from db.updates.apikey import revoke_api_keys
    from model.database import now

    idle_days = configured_idle_days()
    if idle_days <= 0:
        return 0
    current = current or now()
    ids = await idle_key_ids(current - timedelta(days=idle_days))
    if not ids:
        return 0
    return await revoke_api_keys(ids, reason=idle_reason(idle_days), by=SYSTEM_ACTOR)


async def _acquire_sweep_lock(interval_seconds: int) -> bool:
    """多个 gunicorn worker 各有一个巡检循环，用 Redis 锁让每个周期只跑一次。

    拿锁失败（Redis 不可用）时照跑 —— 吊销是幂等的，重复跑只是多一次查询。
    """
    try:
        from utils.cache import get_redis_context

        async with get_redis_context() as redis:
            return bool(await redis.set(SWEEP_LOCK_KEY, "1", ex=max(60, interval_seconds - 30), nx=True))
    except Exception:  # noqa: BLE001
        return True


async def _sweep_loop(interval_seconds: int) -> None:
    import asyncio

    from loguru import logger

    # 启动后先等一会儿，别和部署时的迁移、预热抢连接
    await asyncio.sleep(60)
    while True:
        try:
            if await _acquire_sweep_lock(interval_seconds):
                revoked = await sweep_idle_keys()
                if revoked:
                    logger.warning(f"闲置 API Key 自动吊销: {revoked} 把（阈值 {configured_idle_days()} 天）")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"闲置 API Key 巡检失败（下个周期重试）: {exc}")
        await asyncio.sleep(interval_seconds)


def start_idle_sweeper(interval_seconds: int) -> None:
    """interval_seconds <= 0 关闭巡检（认证时碰到的闲置 Key 仍会当场吊销）"""
    import asyncio

    global _sweep_task
    if interval_seconds <= 0 or _sweep_task is not None or configured_idle_days() <= 0:
        return
    _sweep_task = asyncio.create_task(_sweep_loop(interval_seconds))


def stop_idle_sweeper() -> None:
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        _sweep_task = None
