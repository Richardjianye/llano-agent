"""群聊 agent 的执行租约接口 —— 阶段 27-E

## 这一组接口存在的理由：**没有它，「agent 在群里发言」只能长成客户端自称**

阶段 13 的模块说明写过这件事：开一个「agent 发言」接口的话，它只能是
「客户端说自己是 agent 就当 agent」—— 而那正是决策 #45 反复强调不能做的设计。

租约把这件事翻过来：

1. 主人的 sidecar 来**要**一个 turn（`POST /{code}/agents/{member_id}/turns`）
2. 服务端自己判断有没有该回的事件、谁触发的、有没有权限、跳数到没到、这个项目
   现在能不能开工 —— 全部从库里推，客户端一个字段都提交不了
3. 能开工就发一把租约，`project`、`agent`、主人 uid、触发事件与
   `trigger_request_id` 在那一刻全部锁死
4. 写回时凭租约认身份（`POST /{code}/turns/{id}/reply`），客户端连
   `agent_label` 都不用报，也就没有可伪造的字段

## 单项目单 in-flight 是**跨机器**约束

每个 agent 跑在它主人自己的机器上，A 的 sidecar 不知道 B 的 sidecar 在做什么。
所以这条只能压在数据库的唯一索引上，见 db/inserts/project_turn。

## 熔断在运行时，不靠 prompt

连续 agent-to-agent 默认 8 跳。跳数从消息表尾部数出来，人一说话清零。
写在 prompt 里的「不要一直聊下去」不是熔断 —— 它只在模型愿意听的时候有效。
"""

import uuid
from typing import Any, Dict, List, Optional

from db.inserts.project import post_message
from db.inserts.project_turn import (acquire_turn, cancel_turns, finish_turn,
                                     get_turn, heartbeat_turn,
                                     reclaim_expired_turns, recent_turns)
from db.selects.project import (get_membership, get_project_by_code,
                                list_members)
from db.selects.project_turn import (agent_hop_count,
                                     last_handled_trigger_seq,
                                     pending_mention_triggers,
                                     pending_task_trigger)
from fastapi import APIRouter, Body, Depends, Response
from loguru import logger
from services import project as svc
from services import project_runtime as rt
from utils.errors import AppError
from utils.gateway_auth import Caller, require_caller

router = APIRouter(prefix="/projects")


def _rejected(message: str, *, status: int = 400) -> AppError:
    return AppError(code=10006, message=message, http_status=status)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _project_of(code: str, caller: Caller):
    """定位项目并确认调用者是成员。找不到与没资格给同一个错误。"""
    try:
        normalized = svc.normalize_code(code)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc))

    project = await get_project_by_code(normalized)
    if project is None:
        raise _rejected("项目不存在，或你不是它的成员", status=404)

    member = await get_membership(project["id"], caller.uid)
    if member is None or member.get("status") != svc.ACTIVE:
        raise _rejected("项目不存在，或你不是它的成员", status=404)
    return project, member


def _allowlist_of(agent_member: Dict[str, Any]) -> List[str]:
    import json

    raw = agent_member.get("respond_allowlist")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        # 坏掉的名单当空名单（失败关闭）：解析不了的配置不该换来更松的权限。
        return []
    return [str(uid) for uid in parsed if uid] if isinstance(parsed, list) else []


async def _my_agent(project_id: int, member_id: int, caller: Caller) -> Dict[str, Any]:
    """取出**属于调用者本人的**那个 agent 成员。

    只有主人能替自己的 agent 要 turn。别人替你的 agent 要一个，等于让别人决定
    你的 agent 什么时候说话 —— 而它说的话记在你名下、算你的账。
    """
    for member in await list_members(project_id):
        if int(member.get("id") or 0) != int(member_id):
            continue
        if member.get("member_kind") != svc.KIND_AGENT:
            raise _rejected("这不是一个 agent 成员", status=404)
        if member.get("user_uid") != caller.uid:
            # 与「不存在」同一句话：不告诉对方「有这么个 agent，只是不是你的」。
            raise _rejected("这个 agent 不存在，或不属于你", status=404)
        if member.get("status") != svc.ACTIVE:
            raise _rejected("这个 agent 已经不在项目里了", status=404)
        return member
    raise _rejected("这个 agent 不存在，或不属于你", status=404)


@router.put("/{code}/members/{member_id}/respond-to")
async def set_respond_to(
    code: str,
    member_id: int,
    response: Response,
    respond_to: str = Body(..., embed=True),
    allowlist: Optional[List[str]] = Body(None, embed=True),
    caller: Caller = Depends(require_caller),
):
    """配置这个 agent 接受谁的指令（P0-D）。

    非法值**拒绝**，不静默纠正成默认值 —— 静默纠正等于「你以为设了 owner_only，
    其实是 project_members」。权限设置上的静默纠正一律不可接受。
    """
    import json

    from db import init_async_session
    from model.database import ProjectMember, now
    from sqlalchemy import update

    _no_store(response)
    project, _ = await _project_of(code, caller)
    agent = await _my_agent(project["id"], member_id, caller)

    try:
        mode = rt.normalize_respond_to(respond_to)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc))

    names = [str(uid) for uid in (allowlist or []) if uid]

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(ProjectMember)
            .where(ProjectMember.id == int(agent["id"]))
            .values(
                respond_to=mode,
                respond_allowlist=json.dumps(names, ensure_ascii=False) if names else None,
                updated_at=now(),
            )
        )
        await session.commit()

    if mode == rt.RESPOND_NOBODY:
        # 暂停接受指令时，正在跑的那一轮也要停：只改配置不撤 turn 的话，
        # 「我已经把它关了」和「它还在群里说话」会同时成立。
        await cancel_turns(project_id=project["id"], member_id=int(agent["id"]), reason="respond_to_nobody")

    return {"respond_to": mode, "allowlist": names}


@router.post("/{code}/agents/{member_id}/turns", status_code=201)
async def lease_turn(
    code: str,
    member_id: int,
    response: Response,
    after_seq: int = Body(0, embed=True),
    reason: str = Body(rt.TRIGGER_MENTION, embed=True),
    caller: Caller = Depends(require_caller),
):
    """要一把执行租约。**没有该回的事就明确回「没有」，不发租约。**

    回 `{"turn": null, ...}` 而不是 4xx：轮询到「现在没事做」是正常状态，
    做成错误的话客户端那边每次空转都要 catch 一下，久了就会有人把 catch 写成
    忽略全部错误。
    """
    _no_store(response)
    project, me = await _project_of(code, caller)

    if project["status"] != svc.ACTIVE:
        # 归档项目里不再开新 turn，正在跑的也撤掉。
        await cancel_turns(project_id=project["id"], reason="project_archived")
        return {"turn": None, "stopped": True, "reason": "project_archived"}

    agent = await _my_agent(project["id"], member_id, caller)

    try:
        trigger_reason = rt.normalize_trigger_reason(reason)
    except svc.ProjectRejected as exc:
        # P0 只有四种硬触发。普通消息在 P0 不产生任何模型调用。
        raise _rejected(str(exc), status=403)

    if rt.effective_respond_to(agent) == rt.RESPOND_NOBODY:
        return {"turn": None, "stopped": True, "reason": "respond_to_nobody"}

    # 熔断先判：跳数到了就连事件都不用查了。
    hops = await agent_hop_count(project["id"])
    if rt.hops_exhausted(hops):
        return {"turn": None, "stopped": True, "reason": "hop_limit",
                "hops": hops, "notice": rt.hop_limit_notice()}

    # 过期租约先回收，否则一台崩掉的机器会把项目永久卡住。
    await reclaim_expired_turns(project["id"])

    members = await list_members(project["id"])
    members_by_uid = {
        m["user_uid"]: m for m in members if m.get("member_kind") == svc.KIND_USER
    }
    allowlist = _allowlist_of(agent)

    events: List[rt.TriggerEvent] = []
    task = None

    if trigger_reason == rt.TRIGGER_MENTION:
        # 水位以**服务端记的**为准，客户端只能往后收窄、不能往前放宽。
        #
        # 2026-09-07 真机上的表现：桌面端 tick 每 6 秒拿 `after_seq=0` 来问一次，
        # 而 seq=2 那条 @ 永远在 0 之后 —— 同一条 @ 被反复消费，agent 每 6 秒回
        # 一句，一路刷到 8 跳熔断才停，中间还出现两条一字不差的回复。
        #
        # 客户端的水位靠不住不是因为客户端会写错：它会重启（内存里的水位没了）、
        # 同一个人可能开着两个客户端（两份水位各自推进）。而服务端自己有答案，
        # 见 last_handled_trigger_seq。
        after_seq = max(int(after_seq or 0),
                        await last_handled_trigger_seq(project["id"], int(agent["id"])))
        events = await pending_mention_triggers(
            project_id=project["id"], agent_member=agent, after_seq=after_seq,
            members_by_uid=members_by_uid, allowlist=allowlist,
        )
    elif trigger_reason in {rt.TRIGGER_TASK_ASSIGNED, rt.TRIGGER_TASK_EVENT}:
        task = await pending_task_trigger(project_id=project["id"], agent_member=agent)
        if task is not None:
            events = [rt.TriggerEvent(
                project_id=int(project["id"]), seq=int(task["seq"]),
                reason=trigger_reason, agent_member_id=int(agent["id"]),
                requester_uid=caller.uid, task_id=int(task["id"]),
            )]
    else:  # TRIGGER_OWNER_REQUEST
        # 主人显式要求自己的 agent 说话。调用者已经被证明是主人本人
        # （_my_agent 判过），所以这一条不需要再查事件。
        if not rt.may_trigger(agent, me, allowlist=allowlist) and me.get("user_uid") != agent.get("user_uid"):
            raise _rejected("你不能触发这个 agent", status=403)
        events = [rt.TriggerEvent(
            project_id=int(project["id"]), seq=int(after_seq or 0),
            reason=rt.TRIGGER_OWNER_REQUEST, agent_member_id=int(agent["id"]),
            requester_uid=caller.uid,
        )]

    if not events:
        return {"turn": None, "reason": "no_trigger"}

    head = events[0]
    turn = await acquire_turn(
        project_id=int(project["id"]),
        member_id=int(agent["id"]),
        agent_label=agent.get("agent_label") or "",
        owner_uid=caller.uid,
        trigger_reason=head.reason,
        trigger_seq=head.seq,
        trigger_message_id=head.message_id,
        trigger_task_id=head.task_id,
        requester_uid=head.requester_uid,
        # 发租约的这一刻就生成，不等模型调用 —— 它要串起项目消息、usage
        # 与相关的拒绝日志，而拒绝也可能发生在模型调用之前。
        trigger_request_id=f"turn-{uuid.uuid4().hex[:24]}",
        # 合成上下文可以，合掉审计关联不行：每个事件的 id、seq、发送者与原因
        # 都原样留着。
        batch=[
            {"event_id": e.event_id, "seq": e.seq, "reason": e.reason,
             "requester_uid": e.requester_uid, "message_id": e.message_id,
             "task_id": e.task_id}
            for e in events
        ],
        hop_index=hops,
    )

    if turn is None:
        # 有人在跑。这不是错误 —— 事件留在原地，下一轮再来。
        return {"turn": None, "reason": "project_busy"}

    logger.info(
        f"群聊 turn 开工：{code} agent={agent.get('agent_label')} 主人={caller.uid} "
        f"原因={head.reason} 跳数={hops} rid={turn['trigger_request_id']}"
    )

    return {
        "turn": turn,
        "events": [
            {"event_id": e.event_id, "seq": e.seq, "reason": e.reason,
             "requester_uid": e.requester_uid, "message_id": e.message_id, "task_id": e.task_id}
            for e in events
        ],
        "task": task,
        "hops": hops,
        "hop_limit": rt.MAX_AGENT_HOPS,
        # 客户端拿这个去构造 turn 的工具集。**服务端也会挡一道**（阶段 12 的
        # 作用域闸门），这里给出来只是为了让客户端不必猜。
        "scope": "group",
        "allowed_tool_kinds": sorted(rt.GROUP_TURN_ALLOWED_TOOL_KINDS),
    }


@router.post("/{code}/turns/{turn_id}/heartbeat")
async def turn_heartbeat(
    code: str,
    turn_id: int,
    response: Response,
    status_seq: int = Body(..., embed=True),
    caller: Caller = Depends(require_caller),
):
    """续租并推进状态水位。

    终态不接受任何更新，水位只前进 —— 迟到的心跳不能把已经结束的 turn 重新
    显示成运行中。两条都在 SQL 的 WHERE 里判，不先读后判。
    """
    _no_store(response)
    project, _ = await _project_of(code, caller)
    turn = await get_turn(turn_id)
    if turn is None or int(turn["project_id"]) != int(project["id"]):
        raise _rejected("这个 turn 不存在", status=404)

    accepted = await heartbeat_turn(turn_id=turn_id, owner_uid=caller.uid, status_seq=int(status_seq))
    return {"accepted": accepted, "status": (await get_turn(turn_id) or {}).get("status")}


@router.post("/{code}/turns/{turn_id}/reply", status_code=201)
async def turn_reply(
    code: str,
    turn_id: int,
    response: Response,
    content: str = Body(..., embed=True),
    reply_to_id: Optional[int] = Body(None, embed=True),
    caller: Caller = Depends(require_caller),
):
    """agent 写回一条群聊消息。**身份只从租约取。**

    载荷里没有 `sender_kind`、`owner_uid`、`agent_label` 这些字段，也不会去读
    它们：能自称 agent 的接口就是决策 #45 说不能做的那种设计。

    脱敏照旧在服务端做 —— agent 说的话同样会被别的部门、别的公司的人看到。
    """
    _no_store(response)
    project, _ = await _project_of(code, caller)

    turn = await get_turn(turn_id)
    try:
        # 跨项目（拿项目 A 的租约往 B 写）和跨归属（拿别人的租约冒充别人的
        # agent）都在这里挡，而不是分散在下面的分支里。
        rt.require_lease(turn, project_id=int(project["id"]), uid=caller.uid)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)

    try:
        body = svc.validate_message(content)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc))

    identity = rt.writeback_identity(turn)
    redacted = await svc.redact_for_dept(body, caller.dept_id)
    mentions = svc.parse_mentions(body, await list_members(project["id"]))

    try:
        result = await post_message(
            project_id=int(project["id"]),
            sender_kind=identity.sender_kind,
            # 归属挂主人：群聊消耗算主人的，记账口径才接得上。
            sender_uid=identity.sender_uid,
            sender_username=caller.username,
            sender_agent_label=identity.sender_agent_label,
            dept_id=caller.dept_id,
            dept_code=caller.dept_code,
            content=redacted.text,
            content_original=body if redacted.changed else None,
            redaction_hits=redacted.hits,
            mentions=mentions,
            reply_to_id=reply_to_id,
            # 串起项目消息、usage 与拒绝日志。
            trigger_request_id=identity.trigger_request_id,
            expected_turn_id=turn_id,
        )
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=409)
    return result


@router.post("/{code}/turns/{turn_id}/finish")
async def turn_finish(
    code: str,
    turn_id: int,
    response: Response,
    status: str = Body(rt.TURN_COMPLETED, embed=True),
    error_code: Optional[str] = Body(None, embed=True),
    reply_message_id: Optional[int] = Body(None, embed=True),
    caller: Caller = Depends(require_caller),
):
    """收尾。不调这条的话，项目要等租约过期才能开下一个 turn。"""
    _no_store(response)
    project, _ = await _project_of(code, caller)

    turn = await get_turn(turn_id)
    if turn is None or int(turn["project_id"]) != int(project["id"]):
        raise _rejected("这个 turn 不存在", status=404)
    if turn["owner_uid"] != caller.uid:
        raise _rejected("这个 turn 不属于你", status=403)
    if status not in rt.TURN_TERMINAL:
        raise _rejected("只能收到终态：completed / failed / cancelled")

    ok = await finish_turn(
        turn_id=turn_id, owner_uid=caller.uid, status=status,
        error_code=error_code, reply_message_id=reply_message_id,
    )
    # 已经是终态了：不当错误处理。重复收尾（重试、双击）不该变成一次失败。
    return {"finished": ok, "status": status}


@router.get("/{code}/turns")
async def list_turns(
    code: str,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """agent 活跃状态。界面据此显示 queued / running / completed / failed /
    cancelled，以及某一轮是因为什么停的。"""
    _no_store(response)
    project, _ = await _project_of(code, caller)
    await reclaim_expired_turns(project["id"])
    pending, availability = await pending_states(project)
    return {
        "pending": pending,
        "availability": availability,
        "turns": await recent_turns(project["id"]),
        "hops": await agent_hop_count(project["id"]),
        "hop_limit": rt.MAX_AGENT_HOPS,
    }


@router.post("/{code}/agents/{member_id}/presence")
async def agent_presence(code: str, member_id: int, response: Response,
                         caller: Caller = Depends(require_caller)):
    from utils.cache import get_redis_context
    _no_store(response)
    project, me = await _project_of(code, caller)
    try:
        svc.require_write_access(me)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)
    agent = await _my_agent(project["id"], member_id, caller)
    if project["status"] != svc.ACTIVE or rt.effective_respond_to(agent) == rt.RESPOND_NOBODY:
        return {"online": False}
    async with get_redis_context() as redis:
        await redis.set(f"collab:presence:{project['id']}:{member_id}", "1", ex=20)
    return {"online": True}


async def pending_states(project):
    from utils.cache import get_redis_context
    members = await list_members(project["id"])
    humans = {m["user_uid"]: m for m in members if m.get("member_kind") == svc.KIND_USER}
    agents = [m for m in members if m.get("member_kind") == svc.KIND_AGENT]
    online = {}
    try:
        if agents:
            async with get_redis_context() as redis:
                values = await redis.mget([f"collab:presence:{project['id']}:{m['id']}" for m in agents])
            online = {m["id"]: bool(value) for m, value in zip(agents, values)}
    except Exception:
        # A missing heartbeat is not proof of availability.
        pass
    pending = []
    availability = []
    for agent in agents:
        paused = rt.effective_respond_to(agent) == rt.RESPOND_NOBODY
        availability.append({"member_id": agent["id"], "status": "paused" if paused else "online" if online.get(agent["id"]) else "offline"})
        if paused or project["status"] != svc.ACTIVE:
            continue
        after = await last_handled_trigger_seq(project["id"], agent["id"])
        events = await pending_mention_triggers(project_id=project["id"], agent_member=agent,
            after_seq=after, members_by_uid=humans, allowlist=_allowlist_of(agent))
        for event in events:
            pending.append({"member_id": agent["id"], "agent_label": agent.get("agent_label"),
                "owner_uid": agent["user_uid"], "trigger_seq": event.seq,
                "status": "queued" if online.get(agent["id"]) else "offline"})
    return pending, availability
