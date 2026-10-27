"""API Key 写入"""

from datetime import datetime
from typing import Optional

from db import init_async_session
from model.database import ApiKey
from sqlalchemy import insert


async def insert_api_key(
    user_uid: str,
    name: str,
    key_prefix: str,
    key_hash: str,
    expires_at: Optional[datetime] = None,
    total_token_limit: Optional[int] = None,
    scope: str = "private",
    scope_project_id: Optional[int] = None,
) -> int:
    """签发一把 Key。

    `scope="group"` 是群聊会话令牌（阶段 24）：拿它发的请求，服务端从**凭据**
    就能推出 group，不依赖客户端发头。默认 private，与加这个参数之前一致。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(ApiKey).values(
                user_uid=user_uid,
                name=name,
                key_prefix=key_prefix,
                key_hash=key_hash,
                expires_at=expires_at,
                total_token_limit=total_token_limit,
                scope=scope,
                scope_project_id=scope_project_id,
                status=1,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]
