"""群聊 agent 执行租约的写入 —— 阶段 27-E

## 「一个项目同时只有一个 in-flight turn」压在唯一索引上，不在应用层

每个 agent 跑在**它主人自己的机器上**。先 SELECT 再 INSERT 那种写法在这里必然
撞：两台机器同时查到「现在没人在跑」，然后同时开工，群里出现两个 agent 抢答。

`uq_project_inflight (inflight_key)` 是那把锁：未结束的 turn 写
`inflight_key = project_id`，进终态时置 NULL。MySQL 的唯一索引允许多个 NULL，
所以已结束的 turn 想有多少有多少，在跑的只能有一个。抢不到就是 IntegrityError，
那不是错误，是「有人在跑」这个答案。

## 租约会过期

客户端崩溃、断网、关机时没有人来收尾。没有 `lease_expires_at` 的话，那个项目
就永久卡在「有人在跑」上，而且不会有任何报错。所以每次尝试拿租约之前先回收
过期的 —— 回收写成 `failed(lease_expired)` 而不是删除：静默消失的 turn 会让
「我的 agent 为什么没回」变成一个查不了的问题。
"""

import json
from typing import Any, Dict, List, Optional

from db import init_async_session
from model.database import ProjectAgentTurn, now
from services import project_runtime as rt
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError


def _lease_deadline():
    from datetime import timedelta

    return now() + timedelta(seconds=rt.TURN_LEASE_TTL_SECONDS)


async def reclaim_expired_turns(project_id: int) -> int:
    """回收租约到期的 turn。返回回收条数。

    到期即视为死亡：续不上租约的客户端要么崩了、要么断网了，无论哪种，
    这个项目都不该继续为它停着。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ProjectAgentTurn)
            .where(
                and_(
                    ProjectAgentTurn.project_id == project_id,
                    ProjectAgentTurn.inflight_key.is_not(None),
                    ProjectAgentTurn.lease_expires_at.is_not(None),
                    ProjectAgentTurn.lease_expires_at < now(),
                )
            )
            .values(
                status=rt.TURN_FAILED,
                error_code="lease_expired",
                inflight_key=None,
                finished_at=now(),
                updated_at=now(),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)


async def acquire_turn(
    *,
    project_id: int,
    member_id: int,
    agent_label: str,
    owner_uid: str,
    trigger_reason: str,
    trigger_seq: int,
    requester_uid: str,
    trigger_request_id: str,
    trigger_message_id: Optional[int] = None,
    trigger_task_id: Optional[int] = None,
    batch: Optional[List[Dict[str, Any]]] = None,
    hop_index: int = 0,
) -> Optional[Dict[str, Any]]:
    """抢一把租约。抢不到返回 None（有人在跑），**不抛异常**。

    「有人在跑」是正常状态而不是故障：调用方该做的是把事件留在队列里等下一轮，
    而不是把这一次当成失败去重试。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        try:
            created = await session.execute(
                insert(ProjectAgentTurn).values(
                    project_id=project_id,
                    # 这一列就是锁本身。
                    inflight_key=project_id,
                    member_id=member_id,
                    agent_label=agent_label or "",
                    owner_uid=owner_uid,
                    trigger_reason=trigger_reason,
                    trigger_seq=trigger_seq,
                    trigger_message_id=trigger_message_id,
                    trigger_task_id=trigger_task_id,
                    requester_uid=requester_uid,
                    trigger_request_id=trigger_request_id,
                    batch_json=json.dumps(batch, ensure_ascii=False) if batch else None,
                    hop_index=hop_index,
                    status=rt.TURN_RUNNING,
                    status_seq=1,
                    attempt=1,
                    lease_expires_at=_lease_deadline(),
                    started_at=now(),
                    created_at=now(),
                    updated_at=now(),
                )
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None

        return {
            "id": int(created.inserted_primary_key[0]),
            "project_id": project_id,
            "member_id": member_id,
            "agent_label": agent_label or "",
            "owner_uid": owner_uid,
            "trigger_reason": trigger_reason,
            "trigger_seq": trigger_seq,
            "trigger_request_id": trigger_request_id,
            "hop_index": hop_index,
            "status": rt.TURN_RUNNING,
            "status_seq": 1,
        }


async def heartbeat_turn(*, turn_id: int, owner_uid: str, status_seq: int) -> bool:
    """续租并推进状态水位。

    `status_seq` 只前进，且**终态不接受任何更新** —— 迟到的心跳不能把已经结束的
    turn 重新显示成运行中。两条都写在 WHERE 里，而不是先读后判：先读后判在两台
    机器之间照样会输。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ProjectAgentTurn)
            .where(
                and_(
                    ProjectAgentTurn.id == turn_id,
                    ProjectAgentTurn.owner_uid == owner_uid,
                    ProjectAgentTurn.status == rt.TURN_RUNNING,
                    ProjectAgentTurn.status_seq < status_seq,
                )
            )
            .values(status_seq=status_seq, lease_expires_at=_lease_deadline(), updated_at=now())
        )
        await session.commit()
        return bool(result.rowcount)


async def finish_turn(
    *,
    turn_id: int,
    owner_uid: str,
    status: str,
    error_code: Optional[str] = None,
    reply_message_id: Optional[int] = None,
) -> bool:
    """收尾。把 `inflight_key` 置空，项目才能开下一个 turn。"""
    if status not in rt.TURN_TERMINAL:
        raise ValueError(f"不是终态：{status}")

    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ProjectAgentTurn)
            .where(
                and_(
                    ProjectAgentTurn.id == turn_id,
                    ProjectAgentTurn.owner_uid == owner_uid,
                    ProjectAgentTurn.status == rt.TURN_RUNNING,
                )
            )
            .values(
                status=status,
                error_code=error_code,
                reply_message_id=reply_message_id,
                inflight_key=None,
                finished_at=now(),
                updated_at=now(),
            )
        )
        await session.commit()
        return bool(result.rowcount)


async def cancel_turns(
    *,
    project_id: Optional[int] = None,
    member_id: Optional[int] = None,
    owner_uid: Optional[str] = None,
    reason: str = "cancelled",
) -> int:
    """撤销作用域内所有未结束的 turn。

    项目归档、agent 被移除、账号停用时必须走这里。只停订阅不撤 turn 的话，
    一个已经没资格待在项目里的 agent 仍然会把它这一轮说完 —— 而它说的话会写进
    项目历史，收不回来。
    """
    if project_id is None and member_id is None and owner_uid is None:
        raise ValueError("撤销范围不能为空 —— 那会撤掉全库的 turn")

    conditions = [ProjectAgentTurn.inflight_key.is_not(None)]
    if project_id is not None:
        conditions.append(ProjectAgentTurn.project_id == project_id)
    if member_id is not None:
        conditions.append(ProjectAgentTurn.member_id == member_id)
    if owner_uid is not None:
        conditions.append(ProjectAgentTurn.owner_uid == owner_uid)

    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            update(ProjectAgentTurn)
            .where(and_(*conditions))
            .values(
                status=rt.TURN_CANCELLED,
                error_code=reason,
                inflight_key=None,
                finished_at=now(),
                updated_at=now(),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)


async def get_turn(turn_id: int) -> Optional[Dict[str, Any]]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(select(ProjectAgentTurn).where(ProjectAgentTurn.id == turn_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": int(row.id),
            "project_id": int(row.project_id),
            "member_id": int(row.member_id),
            "agent_label": row.agent_label or "",
            "owner_uid": row.owner_uid,
            "trigger_reason": row.trigger_reason,
            "trigger_seq": int(row.trigger_seq or 0),
            "trigger_message_id": row.trigger_message_id,
            "trigger_task_id": row.trigger_task_id,
            "requester_uid": row.requester_uid,
            "trigger_request_id": row.trigger_request_id,
            "hop_index": int(row.hop_index or 0),
            "status": row.status,
            "status_seq": int(row.status_seq or 0),
            "updated_at": row.updated_at,
        }


async def active_turn(project_id: int) -> Optional[Dict[str, Any]]:
    """这个项目当前在跑的那个 turn（如果有）。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(
                select(ProjectAgentTurn).where(
                    and_(
                        ProjectAgentTurn.project_id == project_id,
                        ProjectAgentTurn.inflight_key.is_not(None),
                    )
                )
            )
        ).scalars().first()
        if row is None:
            return None
        return {
            "id": int(row.id),
            "agent_label": row.agent_label or "",
            "member_id": int(row.member_id),
            "owner_uid": row.owner_uid,
            "status": row.status,
            "status_seq": int(row.status_seq or 0),
            "lease_expires_at": row.lease_expires_at,
        }


async def recent_turns(project_id: int, *, limit: int = 20) -> List[Dict[str, Any]]:
    """最近的 turn，供界面显示 agent 活跃状态。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(ProjectAgentTurn)
                .where(ProjectAgentTurn.project_id == project_id)
                .order_by(ProjectAgentTurn.id.desc())
                .limit(max(1, min(int(limit), 100)))
            )
        ).scalars().all()
        return [
            {
                "id": int(row.id),
                "agent_label": row.agent_label or "",
                "error_code": row.error_code,
                "hop_index": int(row.hop_index or 0),
                "owner_uid": row.owner_uid,
                "status": row.status,
                "trigger_reason": row.trigger_reason,
                "trigger_seq": int(row.trigger_seq or 0),
            }
            for row in rows
        ]


async def count_unfinished(project_id: int) -> int:
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(ProjectAgentTurn.id).where(
                    and_(
                        ProjectAgentTurn.project_id == project_id,
                        or_(
                            ProjectAgentTurn.status == rt.TURN_QUEUED,
                            ProjectAgentTurn.status == rt.TURN_RUNNING,
                        ),
                    )
                )
            )
        ).scalars().all()
        return len(rows)
