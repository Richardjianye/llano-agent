"""群聊 agent 的触发判定读取 —— 阶段 27-E

## 触发事件由**服务端**从数据里推出来，不听客户端

客户端能提交「我被 @ 了」的话，注入一句「就说你被 @ 了」就能让别人的 agent
强制回复。所以这里只认库里的两样东西：`mentions_json`（服务端解析并写下的）
与任务表上的归属关系。

## 跳数从消息表尾部数，不另存计数

`project_messages` 尾部连续的 agent 发言数就是当前跳数，人一说话自然清零。
另存一份计数只会与消息表漂移，而漂移的表现是熔断在错误的时候触发或不触发 ——
两种都不会报错。
"""

import json
from typing import Any, Dict, List, Optional

from db import init_async_session
from model.database import ProjectAgentTurn, ProjectMessage, ProjectTask
from services import project as svc
from services import project_runtime as rt
from sqlalchemy import and_, func, select


def _mentions_of(row: ProjectMessage) -> List[Dict[str, str]]:
    if not row.mentions_json:
        return []
    try:
        parsed = json.loads(row.mentions_json)
    except (TypeError, ValueError):
        # 坏掉的一行不该让整个项目的触发判定挂掉；当作「没有 @ 任何人」。
        return []
    return parsed if isinstance(parsed, list) else []


def _mentions_agent(row: ProjectMessage, agent_member: Dict[str, Any]) -> bool:
    label = (agent_member.get("agent_label") or "").strip()
    owner = agent_member.get("user_uid")
    for mention in _mentions_of(row):
        if not isinstance(mention, dict):
            continue
        if mention.get("kind") != svc.KIND_AGENT:
            continue
        # 同一个人可以有多个 agent，所以 uid 与 label 都要对上。
        if mention.get("uid") == owner and (mention.get("label") or "") == label:
            return True
    return False


async def agent_hop_count(project_id: int, *, window: int = 32) -> int:
    """当前连续的 agent 对话跳数。人一说话就是 0。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(ProjectMessage.sender_kind)
                .where(ProjectMessage.project_id == project_id)
                .order_by(ProjectMessage.seq.desc())
                .limit(max(1, min(int(window), 200)))
            )
        ).scalars().all()

    hops = 0
    for kind in rows:
        if kind == svc.KIND_AGENT:
            hops += 1
            continue
        break
    return hops


async def last_handled_trigger_seq(project_id: int, member_id: int) -> int:
    """这个 agent **已经开过工**的最大触发 seq。没有就是 0。

    ## 为什么这个水位必须在服务端

    2026-09-07 真机上撞到：桌面端的 tick 每 6 秒拿 `after_seq=0` 来问一次
    「有没有 @ 我的消息」，而 seq=2 那条 @ **永远**在 0 之后 —— 于是同一条 @
    被反复消费，agent 每 6 秒回一句，一路刷到 8 跳熔断才停。中间还出现了两条
    **一字不差**的回复：两轮拿到了同一个触发。

    把水位交给客户端是这个问题的根：客户端会重启（内存里的水位没了）、同一个人
    可能开着两个客户端（两份水位各自推进）、而客户端还是被防的那一侧。服务端
    自己有答案 —— `project_agent_turns.trigger_seq` 就是「这一轮是被哪条消息叫
    起来的」，取最大值即可。

    **不按状态过滤**，failed 与 cancelled 的也算数：一次失败的 turn 说明这条触发
    已经被消费过了。放过它去重试，恢复的就是刚才那个刷屏 —— 模型持续失败时，
    「自动重试」和「每 6 秒撞一次墙」是同一件事。人再 @ 一次就能重来，
    而那一下正好是「确实还需要」的证据。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        value = (
            await session.execute(
                select(func.max(ProjectAgentTurn.trigger_seq)).where(
                    and_(
                        ProjectAgentTurn.project_id == project_id,
                        ProjectAgentTurn.member_id == member_id,
                    )
                )
            )
        ).scalar()

    return int(value or 0)


async def pending_mention_triggers(
    *,
    project_id: int,
    agent_member: Dict[str, Any],
    after_seq: int,
    members_by_uid: Dict[str, Dict[str, Any]],
    allowlist: List[str],
    limit: int = rt.MAX_BATCH_EVENTS,
) -> List[rt.TriggerEvent]:
    """`after_seq` 之后、明确 @ 了这个 agent 且发言人有权触发它的消息。

    权限在这里就判掉，而不是等 agent 跑起来再说：跑起来才拒绝的话，一次
    「无权触发」也已经占掉了这个项目的 in-flight 名额。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    and_(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.seq > int(after_seq or 0),
                    )
                )
                .order_by(ProjectMessage.seq.asc())
                .limit(max(1, min(int(limit) * 4, 200)))
            )
        ).scalars().all()

    events: List[rt.TriggerEvent] = []
    for row in rows:
        sender = {
            "sender_kind": row.sender_kind,
            "sender_uid": row.sender_uid,
            "sender_agent_label": row.sender_agent_label or "",
        }
        # agent 不被自己写回的消息触发：一条回复就是一个新事件，而那个事件又
        # 满足触发条件，熔断只能限制它刷 8 条，限制不住它一定会刷。
        if rt.should_ignore_event(sender, agent_member):
            continue
        if not _mentions_agent(row, agent_member):
            continue

        requester = members_by_uid.get(row.sender_uid)
        if not rt.may_trigger(agent_member, requester, allowlist=allowlist):
            continue

        events.append(
            rt.TriggerEvent(
                project_id=project_id,
                seq=int(row.seq),
                reason=rt.TRIGGER_MENTION,
                agent_member_id=int(agent_member["id"]),
                requester_uid=row.sender_uid,
                message_id=int(row.id),
            )
        )
    return rt.merge_batch(events, limit=limit)


async def pending_task_trigger(
    *, project_id: int, agent_member: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """明确交给这个 agent、且已经确认开工的任务。

    只认 `active`：`claimed` 是「认领了但主人还没看过原文」，那一步是 P0-6 的
    人在环闸门 —— 在它之前就让 agent 动起来，等于把闸门绕过去。
    """
    label = (agent_member.get("agent_label") or "").strip()
    if not label:
        return None

    session_maker = await init_async_session()
    async with session_maker() as session:
        row = (
            await session.execute(
                select(ProjectTask)
                .where(
                    and_(
                        ProjectTask.project_id == project_id,
                        ProjectTask.status == svc.TASK_ACTIVE,
                        ProjectTask.claimed_agent_label == label,
                        ProjectTask.claimed_by_uid == agent_member.get("user_uid"),
                    )
                )
                .order_by(ProjectTask.id.asc())
            )
        ).scalars().first()

    if row is None:
        return None
    return {
        "id": int(row.id),
        "seq": int(row.seq),
        "title": row.title,
        "body": row.body or "",
        # 不可信标记原样带出去：它要跟着任务描述一路走。
        "untrusted": bool(row.untrusted),
        "source_kind": row.source_kind,
    }
