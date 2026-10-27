"""协作项目的写入 —— 阶段 13

## 消息序号靠**行锁 + 水位**发，不靠 max(seq)+1

`SELECT max(seq)+1` 在并发发言下必然撞：两个人同时发，读到同一个 max，
写出两条同号。项目行上有个 `message_seq` 水位，发号时 `UPDATE ... SET
message_seq = message_seq + 1` 拿行锁，再读回来 —— 数据库替我们串行化。

`uq_project_message_seq (project_id, seq)` 是最后一道：水位逻辑要是哪天被改坏，
撞唯一键会直接报错，而不是库里静静躺着两条 seq=7。
"""

from typing import Any, Dict, List, Optional

from db import init_async_session
from model.database import Project, ProjectMember, ProjectMessage, now
from services.project import (ACTIVE, KIND_AGENT, KIND_USER, LEFT, ROLE_OWNER,
                              ProjectRejected)
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
import json


async def create_project(
    *,
    code: str,
    name: str,
    description: str,
    owner_uid: str,
    owner_username: Optional[str],
    dept_id: Optional[int],
    dept_code: Optional[str],
) -> Dict[str, Any]:
    """建项目，并把创建人作为 owner 成员一起写进去。

    两件事必须在同一个事务里：只建项目不建成员的话，创建人自己都进不去这个项目
    （成员判定查的是 project_members），而项目 code 已经被占了 —— 他连重建都做不到。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        try:
            created = await session.execute(
                insert(Project).values(
                    code=code, name=name, description=description,
                    owner_uid=owner_uid, created_by_uid=owner_uid,
                    created_dept_id=dept_id, created_dept_code=dept_code,
                    status=ACTIVE, message_seq=0,
                    created_at=now(), updated_at=now(),
                )
            )
        except IntegrityError:
            await session.rollback()
            raise ProjectRejected(f"项目标识 {code} 已被占用")

        project_id = int(created.inserted_primary_key[0])
        await session.execute(
            insert(ProjectMember).values(
                project_id=project_id, member_kind=KIND_USER,
                user_uid=owner_uid, username=owner_username, agent_label="",
                dept_id=dept_id, dept_code=dept_code,
                role=ROLE_OWNER, status=ACTIVE,
                joined_at=now(), created_at=now(), updated_at=now(),
            )
        )
        await session.commit()
        return {"id": project_id, "code": code}


async def add_member(
    *,
    project_id: int,
    member_kind: str,
    user_uid: str,
    username: Optional[str],
    agent_label: str,
    dept_id: Optional[int],
    dept_code: Optional[str],
    role: str,
) -> Dict[str, Any]:
    """加成员，或把一个已退出的成员重新拉回来。

    重新加入走 UPDATE 而不是插新行：插新行的话同一个人在同一个项目里会有两条记录，
    而历史发言挂在旧的那条上 —— 「这个人什么时候在这个项目里」就答不清楚了。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        existing = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.member_kind == member_kind,
                    ProjectMember.user_uid == user_uid,
                    ProjectMember.agent_label == (agent_label or ""),
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.status == ACTIVE:
                raise ProjectRejected("这个成员已经在项目里了")
            await session.execute(
                update(ProjectMember)
                .where(ProjectMember.id == existing.id)
                .values(status=ACTIVE, role=role, left_at=None, left_reason=None,
                        joined_at=now(), updated_at=now())
            )
            await session.commit()
            return {"id": int(existing.id), "rejoined": True}

        created = await session.execute(
            insert(ProjectMember).values(
                project_id=project_id, member_kind=member_kind,
                user_uid=user_uid, username=username, agent_label=agent_label or "",
                dept_id=dept_id, dept_code=dept_code,
                role=role, status=ACTIVE,
                joined_at=now(), created_at=now(), updated_at=now(),
            )
        )
        await session.commit()
        return {"id": int(created.inserted_primary_key[0]), "rejoined": False}


async def remove_member(*, member_id: int, reason: str = "") -> bool:
    """移除成员。**不删行** —— 历史发言还挂在它上面，删了那些发言就没有主语了。

    而且退群只解决可见性：已流入群聊历史的内容收不回来，退出不能当补救手段用。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        member = (await session.execute(select(ProjectMember).where(ProjectMember.id == member_id))).scalar_one_or_none()
        if member is None:
            return False
        project = (await session.execute(select(Project).where(Project.id == member.project_id).with_for_update())).scalar_one()
        await session.refresh(member)
        if member.member_kind != KIND_AGENT and member.role == ROLE_OWNER and project.status == ACTIVE:
            owners = (await session.execute(select(ProjectMember.id).where(
                ProjectMember.project_id == project.id, ProjectMember.member_kind != KIND_AGENT,
                ProjectMember.role == ROLE_OWNER, ProjectMember.status == ACTIVE,
                ProjectMember.id != member_id))).scalars().all()
            if not owners:
                raise ProjectRejected("最后一位负责人请先移交项目或归档，再退出")
        result = await session.execute(
            update(ProjectMember)
            .where(ProjectMember.id == member_id, ProjectMember.status == ACTIVE)
            .values(status=LEFT, left_at=now(), left_reason=(reason or "").strip()[:255] or None,
                    updated_at=now())
        )
        await session.commit()
        return result.rowcount > 0


async def remove_agents_of(*, project_id: int, user_uid: str) -> int:
    """把某人的 agent 一并请出项目。

    "被移除/退出时 agent 自动退出" —— 人走了 agent 还在群里，等于留了一个持有
    主人凭据、还在读项目上下文的成员。**这一步不能靠调用方记得做**，所以移除人
    成员的路径必定连带调用它。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_uid == user_uid,
                ProjectMember.member_kind == KIND_AGENT,
                ProjectMember.status == ACTIVE,
            )
            .values(status=LEFT, left_at=now(), left_reason="主人已退出项目", updated_at=now())
        )
        await session.commit()
        return int(result.rowcount)


async def post_message(
    *,
    project_id: int,
    sender_kind: str,
    sender_uid: str,
    sender_username: Optional[str],
    sender_agent_label: str,
    dept_id: Optional[int],
    dept_code: Optional[str],
    content: str,
    content_original: Optional[str],
    redaction_hits: int,
    mentions: List[Dict[str, str]],
    reply_to_id: Optional[int] = None,
    trigger_request_id: Optional[str] = None,
    expected_turn_id: Optional[int] = None,
    expected_task_id: Optional[int] = None,
) -> Dict[str, Any]:
    """写一条群聊消息，顺便发号。

    `content` 是**脱敏后**的，`content_original` 只在真的替换过时才存 ——
    两份一样时留一份就够了，而且少一份原文就少一处泄漏面。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        task_result = None
        if expected_task_id is not None:
            from model.database import ProjectTask
            project = (await session.execute(select(Project).where(Project.id == project_id).with_for_update())).scalar_one()
            member = (await session.execute(select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_uid == sender_uid,
                ProjectMember.member_kind == KIND_USER, ProjectMember.status == ACTIVE))).scalar_one_or_none()
            if project.status != ACTIVE or member is None or member.role == "viewer":
                raise ProjectRejected("当前不能向该项目提交结果")
            task_result = (await session.execute(select(ProjectTask).where(
                ProjectTask.id == expected_task_id, ProjectTask.project_id == project_id).with_for_update())).scalar_one_or_none()
            if task_result is None or task_result.claimed_by_uid != sender_uid:
                raise ProjectRejected("只有认领人可以提交结果")
            if task_result.status == "submitted" and task_result.result_message_id:
                existing = (await session.execute(select(ProjectMessage).where(ProjectMessage.id == task_result.result_message_id))).scalar_one()
                return {"id": existing.id, "seq": existing.seq, "redacted": bool(existing.redaction_hits), "mentions": []}
            if task_result.status != "active":
                raise ProjectRejected("请先确认开工，再提交结果")
        if expected_turn_id is not None:
            from model.database import ProjectAgentTurn
            from services import project_runtime as rt
            # Serialize final publication with cancellation and archival. A lease
            # checked before redaction may already have been cancelled afterwards.
            project = (await session.execute(select(Project).where(Project.id == project_id).with_for_update())).scalar_one()
            if project.status != ACTIVE:
                raise ProjectRejected("项目已经归档")
            turn = (await session.execute(select(ProjectAgentTurn).where(ProjectAgentTurn.id == expected_turn_id).with_for_update())).scalar_one_or_none()
            # Removal and turn cancellation are separate writes. Recheck both
            # identities under the same project lock so publication cannot slip
            # between them after the owner or Agent has left.
            owner = (await session.execute(select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_uid == sender_uid,
                ProjectMember.member_kind == KIND_USER, ProjectMember.status == ACTIVE
            ).with_for_update())).scalar_one_or_none()
            agent = None if turn is None else (await session.execute(select(ProjectMember).where(
                ProjectMember.id == turn.member_id, ProjectMember.project_id == project_id,
                ProjectMember.member_kind == KIND_AGENT, ProjectMember.status == ACTIVE
            ).with_for_update())).scalar_one_or_none()
            if owner is None or owner.role == "viewer" or agent is None:
                raise ProjectRejected("成员或 Agent 已退出项目")
            rt.require_lease(None if turn is None else {
                "project_id": turn.project_id, "owner_uid": turn.owner_uid, "status": turn.status,
            }, project_id=project_id, uid=sender_uid)
        # 发号：UPDATE 拿行锁，数据库替我们串行化。
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(message_seq=Project.message_seq + 1, updated_at=now())
        )
        seq = int(
            (
                await session.execute(select(Project.message_seq).where(Project.id == project_id))
            ).scalar_one()
        )

        created = await session.execute(
            insert(ProjectMessage).values(
                project_id=project_id, seq=seq,
                sender_kind=sender_kind, sender_uid=sender_uid,
                sender_username=sender_username,
                sender_agent_label=sender_agent_label or None,
                sender_dept_id=dept_id, sender_dept_code=dept_code,
                reply_to_id=reply_to_id,
                mentions_json=json.dumps(mentions, ensure_ascii=False) if mentions else None,
                content=content,
                content_original=content_original,
                redaction_hits=redaction_hits,
                visibility="project",
                trigger_request_id=trigger_request_id,
                created_at=now(),
            )
        )
        if task_result is not None:
            task_result.result_message_id = int(created.inserted_primary_key[0])
            task_result.status = "submitted"
            task_result.updated_at = now()
        await session.commit()
        return {
            "id": int(created.inserted_primary_key[0]),
            "seq": seq,
            "redacted": redaction_hits > 0,
            "mentions": mentions,
        }


# ==================== 任务与认领（P0-6）====================


async def create_task(
    *,
    project_id: int,
    title: str,
    body: str,
    source_kind: str,
    source_message_id: Optional[int],
    untrusted: bool,
    created_by_uid: str,
    created_by_username: Optional[str],
) -> Dict[str, Any]:
    """建任务。任务号与消息序号同一套发号办法（行锁 + 水位）。

    这里复用 projects.message_seq 之外的另一个计数：任务号靠
    `max(seq)+1` 会在并发下撞，而 uq_project_task_seq 会把它变成一次失败
    而不是两条同号 —— 但那对用户是一次莫名其妙的报错。所以同样用 UPDATE 拿锁。
    """
    from model.database import Project, ProjectTask

    session_maker = await init_async_session()
    async with session_maker() as session:
        # 借用同一个水位列。任务号与消息号共用一个序列，号会跳（发一条消息、
        # 建一个任务，任务号可能是 7 而不是 1）—— 但它只是给人念的标识，
        # 连续性没有价值，而多一个计数列就多一处要维护的并发点。
        await session.execute(
            update(Project).where(Project.id == project_id)
            .values(message_seq=Project.message_seq + 1, updated_at=now())
        )
        seq = int(
            (await session.execute(select(Project.message_seq).where(Project.id == project_id)))
            .scalar_one()
        )

        created = await session.execute(
            insert(ProjectTask).values(
                project_id=project_id, seq=seq,
                title=title, body=body or None,
                source_kind=source_kind, source_message_id=source_message_id,
                untrusted=untrusted,
                created_by_uid=created_by_uid, created_by_username=created_by_username,
                status="open", returned_count=0,
                created_at=now(), updated_at=now(),
            )
        )
        await session.commit()
        return {"id": int(created.inserted_primary_key[0]), "seq": seq, "untrusted": untrusted}


async def _task_writer_active(session, task_id: int, uid: str) -> bool:
    """Serialize task claims with membership removal and project archival."""
    from model.database import ProjectTask
    project_id = (await session.execute(select(ProjectTask.project_id).where(ProjectTask.id == task_id))).scalar_one_or_none()
    if project_id is None:
        return False
    project = (await session.execute(select(Project).where(Project.id == project_id).with_for_update())).scalar_one_or_none()
    member = (await session.execute(select(ProjectMember).where(
        ProjectMember.project_id == project_id, ProjectMember.user_uid == uid,
        ProjectMember.member_kind == KIND_USER, ProjectMember.status == ACTIVE
    ).with_for_update())).scalar_one_or_none()
    return project is not None and project.status == ACTIVE and member is not None and member.role != "viewer"


async def claim_task(
    *, task_id: int, uid: str, username: Optional[str], agent_label: str
) -> bool:
    """认领。**只改到 claimed，不改到 active** —— 认领 ≠ 自动开工。

    WHERE 里带 `status == open` 是乐观锁：两个人同时认领，只有一个的 rowcount
    是 1，另一个拿到 0 并被告知已被认领。先查后写会让两个人都认领成功。
    """
    from model.database import ProjectTask
    from services.project import TASK_CLAIMED, TASK_OPEN

    session_maker = await init_async_session()
    async with session_maker() as session:
        if not await _task_writer_active(session, task_id, uid):
            return False
        result = await session.execute(
            update(ProjectTask)
            .where(ProjectTask.id == task_id, ProjectTask.status == TASK_OPEN)
            .values(status=TASK_CLAIMED, claimed_by_uid=uid, claimed_by_username=username,
                    untrusted=ProjectTask.untrusted | (ProjectTask.created_by_uid != uid),
                    claimed_agent_label=(agent_label or "").strip()[:64] or None,
                    claimed_at=now(), confirmed_at=None, updated_at=now())
        )
        await session.commit()
        return result.rowcount > 0


async def confirm_task(*, task_id: int, uid: str) -> bool:
    """主人显式确认开工。**这是 P0-6 那道人在环闸门的落点。**

    WHERE 里同时卡 `status == claimed` 和 `claimed_by_uid == uid`：别人替你确认
    等于替你决定「用你的凭据去执行这段来路不明的文字」。
    """
    from model.database import ProjectTask
    from services.project import TASK_ACTIVE, TASK_CLAIMED

    session_maker = await init_async_session()
    async with session_maker() as session:
        if not await _task_writer_active(session, task_id, uid):
            return False
        result = await session.execute(
            update(ProjectTask)
            .where(ProjectTask.id == task_id, ProjectTask.status == TASK_CLAIMED,
                   ProjectTask.claimed_by_uid == uid)
            .values(status=TASK_ACTIVE, confirmed_at=now(), updated_at=now(),
                    untrusted=ProjectTask.untrusted | (ProjectTask.created_by_uid != uid))
        )
        await session.commit()
        return result.rowcount > 0


async def release_tasks_of(*, user_uid: str, project_id: Optional[int] = None) -> int:
    """把某人认领的、**还没完成**的任务退回未认领池。

    决策 #46：「停用账号时，该 agent 已认领但未完成的任务要自动退回未认领池并
    @ 项目负责人，否则任务永久卡在一个停用账号名下。」

    退回时清掉认领信息但**保留 returned_count** —— 一个被退回过三次的任务，
    多半是描述有问题，那个数是要给项目负责人看的。
    """
    from model.database import ProjectTask
    from services.project import TASK_ACTIVE, TASK_CLAIMED, TASK_OPEN

    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = (
            update(ProjectTask)
            .where(
                ProjectTask.claimed_by_uid == user_uid,
                ProjectTask.status.in_([TASK_CLAIMED, TASK_ACTIVE]),
            )
            .values(status=TASK_OPEN, claimed_by_uid=None, claimed_by_username=None,
                    claimed_agent_label=None, claimed_at=None, confirmed_at=None,
                    returned_count=ProjectTask.returned_count + 1, updated_at=now())
        )
        if project_id is not None:
            stmt = stmt.where(ProjectTask.project_id == project_id)
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount)
