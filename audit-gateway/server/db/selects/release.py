"""版本清单查询

`latest_release_for` 是客户端每次开机都会打的那条路径，所以它只做一件事：
在同一 (platform, arch) 下按版本号取最高的一条**上架**记录。

排序刻意放在 Python 里做，不写成 `ORDER BY version DESC`：版本号在库里是
字符串，MySQL 会按字典序排，`1.10.0` 排在 `1.9.9` 前面 —— 那意味着发了
1.10.0 之后，所有人拿到的「最新版」还是 1.9.9，而且不报错。
候选集是「某平台某架构的上架版本」，量级是个位数，全取回来排完全够用。
"""

from typing import List, Optional

from db import init_async_session
from model.database import ClientRelease
from services.release_manifest import ManifestError, parse_version
from sqlalchemy import select


def _sort_key(row: ClientRelease):
    try:
        return parse_version(row.version)
    except ManifestError:
        # 库里出现语法不合法的版本号只可能来自手工改表。
        # 排到最后而不是抛：一条脏数据不该让所有人更新不了。
        return (-1, 0, 0, 0)


async def get_release(release_id: int) -> Optional[ClientRelease]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(ClientRelease).where(ClientRelease.id == release_id)
            )
        ).scalar_one_or_none()


async def get_release_by_target(
    version: str, platform: str, arch: str
) -> Optional[ClientRelease]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(ClientRelease).where(
                    ClientRelease.version == version,
                    ClientRelease.platform == platform,
                    ClientRelease.arch == arch,
                )
            )
        ).scalar_one_or_none()


async def list_releases(
    platform: Optional[str] = None,
    arch: Optional[str] = None,
    include_disabled: bool = True,
) -> List[ClientRelease]:
    """后台列表：按版本号从新到旧。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(ClientRelease)
        if platform:
            stmt = stmt.where(ClientRelease.platform == platform)
        if arch:
            stmt = stmt.where(ClientRelease.arch == arch)
        if not include_disabled:
            stmt = stmt.where(ClientRelease.status == 1)
        rows = list((await session.execute(stmt)).scalars().all())
    return sorted(rows, key=_sort_key, reverse=True)


async def latest_release_for(platform: str, arch: str) -> Optional[ClientRelease]:
    """该平台架构上架版本里版本号最高的一条；没有就返回 None。"""
    rows = await list_releases(platform=platform, arch=arch, include_disabled=False)
    return rows[0] if rows else None
