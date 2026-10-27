"""部门写入"""

from typing import Optional

from db import init_async_session
from model.database import Department
from sqlalchemy import insert


async def insert_department(
    code: str,
    name: str,
    parent_id: Optional[int] = None,
    remark: Optional[str] = None,
    status: int = 1,
) -> int:
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(Department).values(
                code=code,
                name=name,
                parent_id=parent_id,
                remark=remark,
                status=status,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]
