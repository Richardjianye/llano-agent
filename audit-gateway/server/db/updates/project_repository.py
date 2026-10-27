"""项目仓库状态写入。远端调用永远不持有数据库事务。"""

from db import init_async_session
from model.database import Project, now
from sqlalchemy import update


async def mark_repository_provisioning(project_id: int, repo_name: str) -> bool:
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(Project)
            .where(Project.id == project_id, Project.git_repo_status != "ready")
            .values(
                git_repo_name=repo_name,
                git_repo_status="provisioning",
                git_repo_error=None,
                git_repo_attempted_at=now(),
                updated_at=now(),
            )
        )
        await session.commit()
        return result.rowcount > 0


async def mark_repository_ready(
    project_id: int, *, repo_id: int, repo_name: str, repo_url: str
) -> None:
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(
                git_repo_id=repo_id,
                git_repo_name=repo_name,
                git_repo_url=repo_url,
                git_repo_status="ready",
                git_repo_error=None,
                git_repo_attempted_at=now(),
                updated_at=now(),
            )
        )
        await session.commit()


async def mark_repository_failed(project_id: int, error: str) -> None:
    """晚到的失败不能覆盖另一个 worker 已经写下的 ready。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(Project)
            .where(Project.id == project_id, Project.git_repo_status != "ready")
            .values(
                git_repo_status="failed",
                git_repo_error=(error or "未知错误")[:512],
                git_repo_attempted_at=now(),
                updated_at=now(),
            )
        )
        await session.commit()


async def mark_repository_pending(project_id: int) -> bool:
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(Project)
            .where(Project.id == project_id, Project.git_repo_status != "ready")
            .values(git_repo_status="pending", git_repo_error=None, updated_at=now())
        )
        await session.commit()
        return result.rowcount > 0
