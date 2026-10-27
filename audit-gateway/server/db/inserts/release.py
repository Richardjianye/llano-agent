"""版本清单写入

只有一个入口，而且它**要求调用方先把清单验过签**。签名校验放在 router 层
（见 router/admin/release.py），这里不重复校验也不兜底 —— 两处都做校验会
让「到底谁负责」变得模糊，真正危险的是两处都以为对方做了。
"""

import json
from typing import Any, Mapping

from db import init_async_session
from model.database import ClientRelease
from sqlalchemy import insert


async def insert_release(
    *,
    manifest: Mapping[str, Any],
    signature: str,
    public_key_fp: str,
    published_by: str,
) -> int:
    """写入一条已验签的版本清单。

    manifest 必须是 `normalize_manifest()` 的输出：入库的 manifest_json 就是
    客户端将来拿去验签的那份原文，这里再做任何加工都会让签名对不上。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(ClientRelease).values(
                manifest_json=json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ),
                signature=signature,
                public_key_fp=public_key_fp,
                version=manifest["version"],
                platform=manifest["platform"],
                arch=manifest["arch"],
                policy=manifest["policy"],
                min_supported_version=manifest["min_supported_version"],
                artifact_url=manifest["artifact_url"],
                artifact_sha256=manifest["artifact_sha256"],
                artifact_size=manifest["artifact_size"],
                released_at=manifest["released_at"],
                release_notes=manifest["release_notes"] or None,
                status=1,
                published_by=published_by,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]
