"""项目仓库编排：本地项目先成功，远端仓库后台补齐。"""

import asyncio
from typing import Set

from config import get_settings
from db.selects.project import (get_project_by_id,
                                list_projects_needing_repository)
from db.updates.project_repository import (mark_repository_failed,
                                           mark_repository_provisioning,
                                           mark_repository_ready)
from loguru import logger
from services.gitea import GiteaClient, GiteaError

_tasks: Set[asyncio.Task] = set()


def repository_name(project: dict) -> str:
    """ID 保证稳定与唯一；code 保留可读性，改显示名不影响远端地址。"""
    return f"project-{int(project['id'])}-{project['code']}"


async def provision_project_repository(project_id: int) -> None:
    project = await get_project_by_id(project_id)
    if project is None or project.get("git_repo_status") == "ready":
        return

    name = project.get("git_repo_name") or repository_name(project)
    if not await mark_repository_provisioning(project_id, name):
        return

    settings = get_settings()
    try:
        client = GiteaClient(
            base_url=settings.gitea_base_url or "",
            token=settings.gitea_api_token or "",
            owner=settings.gitea_repo_owner,
            timeout=settings.gitea_timeout_seconds,
        )
        repo = await client.ensure_private_repository(
            name=name,
            description=project.get("description") or project.get("name") or "",
        )
        await mark_repository_ready(
            project_id, repo_id=repo.id, repo_name=repo.name, repo_url=repo.html_url
        )
        logger.info(f"项目仓库已就绪：{project['code']} -> {repo.html_url}")
    except GiteaError as exc:
        await mark_repository_failed(project_id, str(exc))
        logger.warning(f"项目仓库创建失败：{project['code']}（{exc}）")
    except Exception:
        await mark_repository_failed(project_id, "仓库创建发生内部错误")
        logger.exception(f"项目仓库创建异常：{project['code']}")


def schedule_project_repository(project_id: int) -> None:
    task = asyncio.create_task(provision_project_repository(project_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def schedule_pending_repositories() -> int:
    projects = await list_projects_needing_repository()
    for project in projects:
        schedule_project_repository(int(project["id"]))
    return len(projects)


async def stop_repository_tasks() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
