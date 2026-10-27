"""版本清单变更

只允许上架/下架和删除。**签名字段和清单内容一律不可改**：改了内容签名就对
不上，改了签名等于凭空造一个签名 —— 网关既然签不出清单，就也不该有「修改
已发布清单」这种接口。要改内容就下架旧条目、用签名机重新签一版发上来。
"""

from db import init_async_session
from model.database import ClientRelease
from sqlalchemy import delete, update


async def set_release_status(release_id: int, status: int) -> int:
    """上架（1）/ 下架（0）。下架后客户端立刻不再拿到这一版。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ClientRelease)
            .where(ClientRelease.id == release_id)
            .values(status=1 if status else 0)
        )
        await session.commit()
        return result.rowcount


async def delete_release(release_id: int) -> int:
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            delete(ClientRelease).where(ClientRelease.id == release_id)
        )
        await session.commit()
        return result.rowcount
