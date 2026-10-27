"""API Key 更新

所有吊销都只动 status=1 的行：已经吊销的 Key 再被「吊销全部」扫到时，不能把第一次
吊销的时间和原因覆盖掉 —— 那正是事后排查要看的东西。
"""

from typing import Iterable, Optional

from db import init_async_session
from model.database import ApiKey, now
from sqlalchemy import update


def _revoked(reason: str, by: Optional[str]) -> dict:
    return {"status": 0, "revoked_at": now(), "revoke_reason": reason, "revoked_by": by}


async def revoke_api_key(key_id: int, reason: str = "admin", by: Optional[str] = None) -> None:
    """吊销（不物理删，保留审计追溯）"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.status == 1)
            .values(_revoked(reason, by))
        )
        await session.commit()


async def revoke_api_keys(key_ids: Iterable[int], reason: str, by: Optional[str] = None) -> int:
    """批量吊销，返回实际从有效变为吊销的条数"""
    ids = sorted({int(i) for i in key_ids})
    if not ids:
        return 0
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ApiKey)
            .where(ApiKey.id.in_(ids), ApiKey.status == 1)
            .values(_revoked(reason, by))
        )
        await session.commit()
        return int(result.rowcount or 0)


async def revoke_user_api_keys(
    user_uid: str, reason: str = "account_disabled", by: Optional[str] = None
) -> None:
    """吊销账号的全部设备 Key（停用账号/重置密码时使用）。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.user_uid == user_uid, ApiKey.status == 1)
            .values(_revoked(reason, by))
        )
        await session.commit()


async def touch_api_key(key_id: int) -> None:
    """更新最后使用时间

    调用方用 fire-and-forget 方式触发：这是统计信息，
    不值得为它在转发热路径上多等一次数据库往返。
    （2026-10-19 起它也是闲置失效的依据，见 services/apikey_lifecycle.py。）
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now())
        )
        await session.commit()
