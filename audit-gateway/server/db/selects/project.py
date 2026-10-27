"""协作项目的读取 —— 阶段 13"""

from typing import Any, Dict, List, Optional

from db import init_async_session
from model.database import Project, ProjectMember, ProjectMessage, User
from services.project import ACTIVE, KIND_AGENT
from sqlalchemy import select


def _project_payload(p: Project) -> Dict[str, Any]:
    return {
        "id": int(p.id),
        "code": p.code,
        "name": p.name,
        "description": p.description or "",
        "owner_uid": p.owner_uid,
        "message_seq": int(p.message_seq or 0),
        "status": p.status,
        "git_repo_url": p.git_repo_url or "",
        "git_repo_id": int(p.git_repo_id) if p.git_repo_id is not None else None,
        "git_repo_name": p.git_repo_name or "",
        "git_repo_status": p.git_repo_status or "pending",
        "git_repo_error": p.git_repo_error or "",
        "created_at": p.created_at.isoformat(timespec="seconds") if p.created_at else "",
        "updated_at": p.updated_at.isoformat(timespec="seconds") if p.updated_at else "",
    }


def _member_payload(m: ProjectMember) -> Dict[str, Any]:
    return {
        "id": int(m.id),
        "member_kind": m.member_kind,
        "user_uid": m.user_uid,
        "username": m.username or "",
        "agent_label": m.agent_label or "",
        "dept_code": m.dept_code or "",
        "role": m.role,
        "status": m.status,
        # 触发范围（阶段 27）。只对 agent 成员有意义 —— 界面据此显示「谁可以
        # 指挥它」。**名单本身不外发**：那是一串同事的 uid，读接口不该顺手把它
        # 广播给项目里的每一个人。
        "respond_to": getattr(m, "respond_to", None) or "project_members",
        "respond_allowlist": getattr(m, "respond_allowlist", None),
        "joined_at": m.joined_at.isoformat(timespec="seconds") if m.joined_at else "",
    }


def _message_payload(m: ProjectMessage) -> Dict[str, Any]:
    """给成员看的那份。

    ⚠️ **绝不带 content_original**。那是脱敏前的原文，与 conversation_logs 的原文
    同级敏感 —— 群聊读接口带上它，等于 P0-2 那条防线白做：内容照样原样流到了
    其他部门 / 其他公司的人眼前。原文只走审计侧，且另有权限点。
    """
    return {
        "id": int(m.id),
        "seq": int(m.seq),
        "sender_kind": m.sender_kind,
        "sender_uid": m.sender_uid,
        "sender_username": m.sender_username or "",
        "sender_agent_label": m.sender_agent_label or "",
        "sender_dept_code": m.sender_dept_code or "",
        "reply_to_id": int(m.reply_to_id) if m.reply_to_id else None,
        "content": m.content,
        "redacted": int(m.redaction_hits or 0) > 0,
        "created_at": m.created_at.isoformat(timespec="seconds") if m.created_at else "",
    }


async def get_project_by_code(code: str) -> Optional[Dict[str, Any]]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(select(Project).where(Project.code == code))
        ).scalar_one_or_none()
        return _project_payload(row) if row else None


async def get_project_by_id(project_id: int) -> Optional[Dict[str, Any]]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        return _project_payload(row) if row else None


async def list_projects_needing_repository(limit: int = 100) -> List[Dict[str, Any]]:
    """启动恢复用；ready 之外的项目都可安全重试，远端创建本身幂等。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(Project)
                .where(Project.git_repo_status != "ready")
                .order_by(Project.id)
                .limit(max(1, min(limit, 500)))
            )
        ).scalars().all()
        return [_project_payload(row) for row in rows]


async def get_membership(project_id: int, user_uid: str) -> Optional[Dict[str, Any]]:
    """这个人在这个项目里是什么身份。

    只查**人成员**：agent 的权限从不独立存在，它跟着主人。让 agent 成员单独持有
    角色的话，会出现「主人是只读、他的 agent 能发言」这种没人想要的状态。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_uid == user_uid,
                    ProjectMember.member_kind != KIND_AGENT,
                )
            )
        ).scalar_one_or_none()
        return _member_payload(row) if row else None


async def list_members(project_id: int, *, include_left: bool = False) -> List[Dict[str, Any]]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(ProjectMember, User.username).outerjoin(User, User.uid == ProjectMember.user_uid).where(ProjectMember.project_id == project_id)
        if not include_left:
            stmt = stmt.where(ProjectMember.status == ACTIVE)
        rows = (await session.execute(stmt.order_by(ProjectMember.id))).all()
        result = []
        for member, username in rows:
            payload = _member_payload(member)
            payload["username"] = username or payload["username"]
            result.append(payload)
        return result


async def list_my_projects(user_uid: str) -> List[Dict[str, Any]]:
    """我参与的项目。已退出的不给 —— 退群解决的就是可见性。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(Project, ProjectMember)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(
                    ProjectMember.user_uid == user_uid,
                    ProjectMember.member_kind != KIND_AGENT,
                    ProjectMember.status == ACTIVE,
                )
                .order_by(Project.updated_at.desc())
            )
        ).all()
        out = []
        for project, member in rows:
            payload = _project_payload(project)
            payload["my_role"] = member.role
            out.append(payload)
        return out


async def list_messages(
    project_id: int, *, after_seq: int = 0, limit: int = 50
) -> List[Dict[str, Any]]:
    """按序号往后拉。

    用 seq 而不是 id 做游标：全局 id 会因为**别的项目**的写入而跳号，
    客户端看到号段不连续，像是丢了消息。
    """
    limit = max(1, min(int(limit or 50), 200))
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.seq > int(after_seq or 0),
                )
                .order_by(ProjectMessage.seq)
                .limit(limit)
            )
        ).scalars().all()
        return [_message_payload(r) for r in rows]


# ==================== 任务（P0-6）====================


def _task_payload(t) -> Dict[str, Any]:
    """任务列表的一行。

    ⚠️ **带上 `untrusted`**。前端要靠它把「这条来自群聊」显示出来 —— 而认领人
    在确认开工前看到的那个提醒，是 P0-6 整条防线唯一的人在环环节。
    漏掉这个字段，界面上「群里来的」和「自己写的」长得一模一样。
    """
    return {
        "id": int(t.id),
        "project_id": int(t.project_id),
        "seq": int(t.seq),
        "title": t.title,
        "body": t.body or "",
        "status": t.status,
        "source_kind": t.source_kind,
        "source_message_id": int(t.source_message_id) if t.source_message_id else None,
        "untrusted": bool(t.untrusted),
        "created_by_uid": t.created_by_uid,
        "created_by_username": t.created_by_username or "",
        "result_message_id": int(t.result_message_id) if t.result_message_id else None,
        "claimed_by_uid": t.claimed_by_uid or "",
        "claimed_by_username": t.claimed_by_username or "",
        "claimed_agent_label": t.claimed_agent_label or "",
        "claimed_at": t.claimed_at.isoformat(timespec="seconds") if t.claimed_at else "",
        "confirmed_at": t.confirmed_at.isoformat(timespec="seconds") if t.confirmed_at else "",
        "returned_count": int(t.returned_count or 0),
        "created_at": t.created_at.isoformat(timespec="seconds") if t.created_at else "",
    }


async def list_tasks(project_id: int, *, status: str = "") -> List[Dict[str, Any]]:
    from model.database import ProjectTask

    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(ProjectTask).where(ProjectTask.project_id == project_id)
        if status.strip():
            stmt = stmt.where(ProjectTask.status == status.strip())
        rows = (await session.execute(stmt.order_by(ProjectTask.seq))).scalars().all()
        return [_task_payload(r) for r in rows]


async def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    from model.database import ProjectTask

    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(select(ProjectTask).where(ProjectTask.id == task_id))
        ).scalar_one_or_none()
        return _task_payload(row) if row else None


async def get_message(project_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    factory = await init_async_session()
    async with factory() as session:
        row = (await session.execute(select(ProjectMessage).where(
            ProjectMessage.project_id == project_id, ProjectMessage.id == message_id
        ))).scalar_one_or_none()
        return _message_payload(row) if row else None
