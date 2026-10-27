"""协作项目的客户端接口 —— 阶段 13

## 与 skill-hub 同一套约定：拒绝走真 4xx

调用方是**机器**（桌面客户端与 agent），按 2xx 分支。拒绝要是回 200，一次
「你不是这个项目的成员」会被当成发言成功 —— 员工那边一切正常，群里什么都没有。

## 读接口永远不给原文

`content_original` 是脱敏前的原文，与会话原文同级敏感。群聊读接口带上它等于
P0-2 那条防线白做：内容照样原样流到了其他部门 / 其他公司的人眼前。
原文只走审计侧，且另有权限点。

## ⚠️ 这里**没有**「agent 在群里发言」的接口

agent 发言要先解决两件这一刀没做的事：群级共享一次路由判定（N 个 agent = N 倍
打分成本，MaiBot 是单 bot，我们一个项目群里可能有 5 个），以及 `call_kind=gate`
的记账落地。字段这轮加了，写入路径还没接。

现在开一个「agent 发言」接口的话，它只能长成「客户端说自己是 agent 就当 agent」
—— 而那正是决策 #45 反复强调不能做的那种设计。

阶段 24 补上的是**另一件**前置：`POST /{code}/session` 签发群聊会话令牌，
让服务端从凭据本身推得出 group，不再依赖客户端发头。上面那两件仍然没做。
"""

import json

from db.inserts.project import (add_member, claim_task, confirm_task,
                                create_project, create_task, post_message,
                                release_tasks_of, remove_agents_of,
                                remove_member)
from db.selects.project import (get_membership, get_project_by_code,
                                get_task, get_message, list_members, list_messages,
                                list_my_projects, list_tasks)
from fastapi import APIRouter, Body, Depends, Query, Request, Response
from loguru import logger
from model.database import ROLE_DEVELOPER, ROLE_PRESIDENT
from services import conversation_scope as scope_service
from services import project as svc
from services.project_repository import schedule_project_repository
from utils.errors import AppError
from utils.gateway_auth import Caller, require_caller

router = APIRouter(prefix="/projects")

_ADMIN_ROLES = frozenset({ROLE_PRESIDENT, ROLE_DEVELOPER})


def _rejected(message: str, *, status: int = 400) -> AppError:
    return AppError(code=10006, message=message, http_status=status)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _resolve(code: str, caller: Caller):
    """定位项目并判定调用者身份。找不到与没资格给同一个错误。

    区分开等于告诉外人「有这么个项目，只是你进不去」—— 与 skill hub 下架后统一
    回 404 是同一条理由。
    """
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


@router.get("")
async def my_projects(response: Response, caller: Caller = Depends(require_caller)):
    """我参与的项目。"""
    _no_store(response)
    rows = await list_my_projects(caller.uid)
    for project in rows:
        project["can_manage"] = project.get("my_role") == svc.ROLE_OWNER or caller.role in _ADMIN_ROLES
    return {"projects": rows}


@router.post("", status_code=201)
async def create(
    response: Response,
    code: str = Body(..., embed=True),
    name: str = Body("", embed=True),
    description: str = Body("", embed=True),
    caller: Caller = Depends(require_caller),
):
    """建项目。创建人自动成为 owner 成员。"""
    _no_store(response)
    try:
        normalized = svc.normalize_code(code)
        result = await create_project(
            code=normalized,
            name=(name or normalized).strip()[:255],
            description=(description or "").strip()[:1024],
            owner_uid=caller.uid,
            owner_username=caller.username,
            dept_id=caller.dept_id,
            dept_code=caller.dept_code,
        )
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=409 if "已被占用" in str(exc) else 400)

    logger.info(f"协作项目创建：{normalized}（负责人 {caller.username or caller.uid}）")
    # 本地事务已经提交；远端失败只改变仓库状态，不能把项目一起回滚。
    schedule_project_repository(int(result["id"]))
    return result


@router.post("/{code}/repository/retry", status_code=202)
async def retry_repository(
    code: str,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """负责人或管理员手动重试失败的仓库创建。ready 项目幂等返回。"""
    from db.updates.project_repository import mark_repository_pending

    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)

    if project.get("git_repo_status") == "ready":
        return {
            "status": "ready",
            "git_repo_url": project.get("git_repo_url") or "",
        }

    await mark_repository_pending(int(project["id"]))
    schedule_project_repository(int(project["id"]))
    return {"status": "pending", "git_repo_url": ""}


@router.get("/{code}/members")
async def members(code: str, response: Response, caller: Caller = Depends(require_caller)):
    """成员与 agent。每行带一个 `is_me`。

    客户端要能认出「哪些 agent 是我的」—— 只有主人能替自己的 agent 要 turn，
    也只有主人能改它的触发范围。让客户端自己比 uid 的话，它先得知道自己的 uid，
    而那要么再开一个「我是谁」的接口，要么让它把 uid 存在本地 —— 后者正是
    「身份由客户端提供」的开头。这里服务端顺手判掉，客户端只读一个布尔。
    """
    _no_store(response)
    project, _ = await _resolve(code, caller)
    rows = await list_members(project["id"])
    for row in rows:
        # agent 成员的 user_uid 存的是**主人**的 uid，所以人和 agent 同一条判定。
        row["is_me"] = row.get("user_uid") == caller.uid
        raw_allowlist = row.pop("respond_allowlist", None)
        if row["is_me"] and row.get("member_kind") == svc.KIND_AGENT:
            try:
                row["allowlist"] = json.loads(raw_allowlist or "[]")
            except (TypeError, ValueError):
                row["allowlist"] = []
    return {"members": rows}


@router.post("/{code}/archive")
async def archive(
    code: str,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """归档项目。只有负责人（或管理员）能做。

    归档要**同时撤销这个项目里还在跑的 turn**：只把项目标成归档、不撤 turn 的话，
    一个已经归档的项目里 agent 仍然会把这一轮说完 —— 而它说的话会写进项目历史，
    收不回来。撤销与归档写在同一处，是因为分开写迟早会漏一边。
    """
    from db import init_async_session
    from db.inserts.project_turn import cancel_turns
    from model.database import Project, now
    from sqlalchemy import update

    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(Project).where(Project.id == project["id"])
            .values(status=svc.ARCHIVED, updated_at=now())
        )
        await session.commit()

    cancelled = await cancel_turns(project_id=int(project["id"]), reason="project_archived")
    logger.info(f"项目已归档：{code}（by {caller.uid}，撤销 {cancelled} 个在跑的 turn）")
    return {"status": svc.ARCHIVED, "cancelled_turns": cancelled}


@router.post("/{code}/members", status_code=201)
async def invite(
    code: str,
    response: Response,
    member_kind: str = Body(svc.KIND_USER, embed=True),
    user_uid: str = Body("", embed=True),
    username: str = Body("", embed=True),
    agent_label: str = Body("", embed=True),
    role: str = Body(svc.ROLE_MEMBER, embed=True),
    caller: Caller = Depends(require_caller),
):
    """加成员，或把自己的 agent 拉进群。

    **拉 agent 只能拉自己的**：`member_kind=agent` 时 user_uid 一律用调用者自己的，
    载荷里写别人的 uid 会被忽略。让人能把别人的 agent 拉进群等于替别人的凭据
    做决定 —— 而那个 agent 持有的是它主人的中台账号。
    """
    _no_store(response)
    project, me = await _resolve(code, caller)

    if member_kind == svc.KIND_AGENT:
        target_uid = caller.uid
        target_username = caller.username
        label = (agent_label or "").strip()[:64] or f"{caller.username or caller.uid} 的助手"
        # 拉自己的 agent 不需要 owner 权限，但得先有发言权 —— 一个只读成员的
        # agent 在群里说话，等于绕过了他自己的只读限制。
        try:
            svc.require_write_access(me)
        except svc.ProjectRejected as exc:
            raise _rejected(str(exc), status=403)
        # agent 行上的 role 是**装饰性的**：权限判定走 get_membership，而那个查询
        # 明确排除了 agent —— agent 的权限从不独立存在，它跟着主人。所以这里一律
        # 写 member，而不是抄主人的角色：抄成 owner 会让人以为「这个 agent 是
        # 项目负责人」，而那是一个不存在的概念。
        target_role = svc.ROLE_MEMBER
    else:
        try:
            svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
        except svc.ProjectRejected as exc:
            raise _rejected(str(exc), status=403)
        target_uid = (user_uid or "").strip()
        if not target_uid:
            raise _rejected("要加的成员 uid 不能为空")
        from db.selects.user import get_user_info
        target_user = await get_user_info(uid=target_uid)
        if target_user is None or target_user.status != 1:
            raise _rejected("成员账号不存在或已停用")
        target_username = target_user.username
        label = ""
        target_role = role if role in {svc.ROLE_MEMBER, svc.ROLE_VIEWER, svc.ROLE_OWNER} else svc.ROLE_MEMBER

    try:
        return await add_member(
            project_id=project["id"], member_kind=member_kind,
            user_uid=target_uid, username=target_username, agent_label=label,
            dept_id=caller.dept_id if member_kind == svc.KIND_AGENT else None,
            dept_code=caller.dept_code if member_kind == svc.KIND_AGENT else None,
            role=target_role,
        )
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=409)


@router.delete("/{code}/members/{member_id}")
async def kick(
    code: str,
    member_id: int,
    response: Response,
    reason: str = Query(""),
    caller: Caller = Depends(require_caller),
):
    """移除成员。移除人成员时**连带**把他的 agent 一起请出去。

    人走了 agent 还在群里，等于留了一个持有主人凭据、还在读项目上下文的成员。
    这一步不靠调用方记得做。
    """
    _no_store(response)
    project, me = await _resolve(code, caller)

    all_members = {m["id"]: m for m in await list_members(project["id"])}
    target = all_members.get(int(member_id))
    if target is None:
        raise _rejected("成员不存在", status=404)

    # 自己退出不需要 owner 权限；移除别人才需要。
    leaving_self = target["user_uid"] == caller.uid
    if not leaving_self:
        try:
            svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
        except svc.ProjectRejected as exc:
            raise _rejected(str(exc), status=403)

    try:
        removed = await remove_member(member_id=int(member_id), reason=reason)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=409)
    agents_removed = 0
    tasks_released = 0
    if removed and target["member_kind"] != svc.KIND_AGENT:
        tasks_released = await release_tasks_of(user_uid=target["user_uid"], project_id=project["id"])
        agents_removed = await remove_agents_of(
            project_id=project["id"], user_uid=target["user_uid"]
        )

    if removed:
        # 撤掉这个作用域里还在跑的 turn（阶段 27-E）。只移除成员不撤 turn 的话，
        # 一个已经被请出项目的 agent 仍然会把这一轮说完 —— 而它说的话会写进
        # 项目历史，收不回来。
        from db.inserts.project_turn import cancel_turns

        if target["member_kind"] == svc.KIND_AGENT:
            await cancel_turns(member_id=int(member_id), reason="member_removed")
        else:
            await cancel_turns(
                project_id=int(project["id"]), owner_uid=target["user_uid"],
                reason="member_removed",
            )

    return {"removed": removed, "agents_removed": agents_removed, "tasks_released": tasks_released}


# 群聊会话令牌的有效期。短，因为它是**给一次群聊会话用的**，不是设备 Key。
#
# 但也不能太短：桌面端在群里聊到一半令牌过期，表现是「突然发不出消息」，
# 而重新签发要走一次私聊凭据 —— 那时用户正在群里，手边未必有。
# 8 小时覆盖一个工作日的一段，过夜要重签。
GROUP_SESSION_TTL_HOURS = 8


@router.post("/{code}/session", status_code=201)
async def group_session(
    code: str,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """签发一把**群聊会话令牌**（阶段 24）。

    ## 它是限制，不是授权

    这把令牌不比调用者本人多任何权限 —— 它唯一的作用是让服务端**从凭据本身**
    推出 `group`，从而关掉 MCP 与 skill（决策 #45：能力不跨界，只有产物跨界）。

    这一点决定了它的生命周期可以宽松：成员被移出项目后令牌还没过期，也不会
    让他多拿到任何东西，只会让他少拿到东西。反过来的设计（令牌授予权限）就得
    在每次请求上重查成员关系。

    ## 为什么必须有它

    在此之前「这一轮是群聊」只能靠客户端发 `X-Llano-Conversation-Scope` 声明。
    忘发、或客户端被控制，就静默降级成 private —— 而那正是决策 #45 要挡的路。
    阶段 12 把「只能收紧」这条规则钉死时就说了，另一半等的是这个。

    ## 只有私聊凭据能签

    拿群聊令牌再签一把，等于让持有者自己延长有效期、或换一个项目 —— 前者绕过
    过期，后者是横向越权。而正常流程里签发这一步永远发生在私聊上下文。
    """
    _no_store(response)

    if scope_service.scope_from_credential(caller) != scope_service.PRIVATE:
        # 与其他拒绝同一句话：不告诉对方「你手里这把是群聊令牌」
        raise _rejected(scope_service.SCOPE_DENIED_MESSAGE, status=403)

    project, member = await _resolve(code, caller)
    svc.require_write_access(member)

    from datetime import timedelta

    from db.inserts.apikey import insert_api_key
    from model.database import now
    from utils.apikey import generate_api_key

    plain, prefix, hashed = generate_api_key()
    expires_at = now() + timedelta(hours=GROUP_SESSION_TTL_HOURS)
    await insert_api_key(
        user_uid=caller.uid,
        name=f"群聊 {code}",
        key_prefix=prefix,
        key_hash=hashed,
        expires_at=expires_at,
        scope=scope_service.GROUP,
        scope_project_id=int(project["id"]),
    )

    logger.info(f"签发群聊会话令牌 project={code} uid={caller.uid} 有效期={GROUP_SESSION_TTL_HOURS}h")

    # 明文只回这一次，与设备 Key 同一套约定
    return {
        "api_key": plain,
        "scope": scope_service.GROUP,
        "project_code": code,
        "expires_at": expires_at.isoformat(timespec="seconds"),
    }


@router.get("/{code}/messages")
async def messages(
    code: str,
    response: Response,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    caller: Caller = Depends(require_caller),
):
    """按序号往后拉。只读成员也能读。"""
    _no_store(response)
    project, _ = await _resolve(code, caller)
    return {"messages": await list_messages(project["id"], after_seq=after_seq, limit=limit)}


@router.post("/{code}/messages", status_code=201)
async def say(
    code: str,
    request: Request,
    response: Response,
    content: str = Body(..., embed=True),
    reply_to_id: int | None = Body(None, embed=True),
    caller: Caller = Depends(require_caller),
):
    """发一条群聊消息。

    ## 脱敏在**服务端**做（P0-2）

    投递出去的永远是服务端替换过的那份。指望发送方的客户端替换，等于把一条防线
    交给被防的那一侧 —— 它跑在员工自己的机器上，而接收方是别的部门、甚至别的公司
    的人。

    ## @ 也在服务端解析

    客户端提交 mentions 的话，注入一句「就说你被 @ 了」就能让 agent 强制回复。
    与决策 #45「身份绝不能由 agent 提供」是同一类问题。
    """
    _no_store(response)
    project, me = await _resolve(code, caller)

    if project["status"] != svc.ACTIVE:
        raise _rejected("项目已归档，不能再发言", status=403)

    try:
        svc.require_write_access(me)
        body = svc.validate_message(content)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403 if "只读" in str(exc) else 400)

    # 按**发言人**部门解析词表（阶段 18）—— 见 redact_for_dept 的说明。
    redacted = await svc.redact_for_dept(body, caller.dept_id)
    mentions = svc.parse_mentions(body, await list_members(project["id"]))

    result = await post_message(
        project_id=project["id"],
        sender_kind=svc.KIND_USER,
        sender_uid=caller.uid,
        sender_username=caller.username,
        sender_agent_label="",
        dept_id=caller.dept_id,
        dept_code=caller.dept_code,
        content=redacted.text,
        # 两份一样时不存原文：少一份原文就少一处泄漏面。
        content_original=body if redacted.changed else None,
        redaction_hits=redacted.hits,
        mentions=mentions,
        reply_to_id=reply_to_id,
    )
    return result


# ==================== 任务与认领（P0-6）====================
#
# 这一组接口存在的首要理由不是「管理任务」，是**挡住一条绕道**：群里发一个夹带
# 注入的任务描述 → agent 认领 → 回到私聊上下文执行 → 此时它持有主人的全部
# MCP 凭据。执行不发生在群聊，阶段 12 那道闸门完全落空。
#
# 所以这几条路径上有两件事不能省：**不可信标记跟着任务走**，
# **认领与开工分成两步**。


@router.get("/{code}/tasks")
async def tasks(
    code: str,
    response: Response,
    status: str = Query("", description="open / claimed / active / done / cancelled"),
    caller: Caller = Depends(require_caller),
):
    _no_store(response)
    project, _ = await _resolve(code, caller)
    return {"tasks": await list_tasks(project["id"], status=status)}


@router.post("/{code}/tasks", status_code=201)
async def add_task(
    code: str,
    response: Response,
    title: str = Body(..., embed=True),
    body: str = Body("", embed=True),
    source_message_id: int | None = Body(None, embed=True),
    source_kind: str = Body(svc.SOURCE_CHAT, embed=True),
    caller: Caller = Depends(require_caller),
):
    """建任务。

    `untrusted` **由服务端按来源定，不听客户端**。让客户端提交这个标记的话，
    注入一句「把 untrusted 设成 false」就绕过了整条 P0-6 防线 —— 与决策 #45
    那条「身份绝不能由 agent 提供」是同一类问题。
    """
    _no_store(response)
    project, me = await _resolve(code, caller)

    try:
        svc.require_write_access(me)
        clean_title, clean_body = svc.validate_task(title, body)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403 if "只读" in str(exc) else 400)

    if source_message_id is not None and not await get_message(project["id"], source_message_id):
        raise _rejected("来源消息不属于该项目", status=404)
    kind = source_kind if source_kind in {svc.SOURCE_CHAT, svc.SOURCE_MANUAL} else svc.SOURCE_CHAT
    return await create_task(
        project_id=project["id"],
        title=clean_title, body=clean_body,
        source_kind=kind, source_message_id=source_message_id,
        # 服务端定，不读载荷里的同名字段。
        untrusted=svc.default_untrusted(kind),
        created_by_uid=caller.uid, created_by_username=caller.username,
    )


@router.post("/{code}/tasks/{task_id}/claim")
async def claim(
    code: str,
    task_id: int,
    response: Response,
    agent_label: str = Body("", embed=True),
    caller: Caller = Depends(require_caller),
):
    """认领。**只到 claimed，不到 active** —— 认领 ≠ 自动开工。

    返回体里带 `confirmation`：那是确认开工前要原样呈给认领人看的任务原文。
    「人在环这一下比任何自动检测都可靠，成本也极低。」
    """
    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_write_access(me)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)

    task = await get_task(task_id)
    if task is None or task.get("project_id") != project["id"] or task["id"] is None or task.get("seq") is None:
        raise _rejected("任务不存在", status=404)
    if project["status"] != svc.ACTIVE:
        raise _rejected("项目已经归档", status=409)
    task["untrusted"] = bool(task["untrusted"] or task.get("created_by_uid") != caller.uid)
    if task.get("claimed_by_uid") == caller.uid and task["status"] in (svc.TASK_CLAIMED, svc.TASK_ACTIVE):
        return {"claimed": True, "status": task["status"], "confirmation": svc.confirmation_notice(task)}
    try:
        svc.can_claim(task)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=409)

    if not await claim_task(task_id=task_id, uid=caller.uid,
                            username=caller.username, agent_label=agent_label):
        # 乐观锁没抢到：另一个人在这几毫秒里认领了。
        raise _rejected("这个任务刚被别人认领了", status=409)

    return {
        "claimed": True,
        "status": svc.TASK_CLAIMED,
        # 认领**不等于**可以开工。客户端必须把这份原文呈给人看，拿到确认再调
        # /confirm。字段名叫 confirmation 而不是 preview，是想让忽略它这件事
        # 看起来足够可疑。
        "confirmation": svc.confirmation_notice(task),
        "next": f"确认原文无误后调用 POST /v1/projects/{code}/tasks/{task_id}/confirm",
    }


@router.post("/{code}/tasks/{task_id}/confirm")
async def confirm(
    code: str,
    task_id: int,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """主人显式确认开工 —— P0-6 那道人在环闸门的落点。

    只有认领人本人能确认：别人替你确认等于替你决定「用你的凭据去执行这段来路
    不明的文字」，而整条防线就架在「认领人自己看过原文」这一下上。
    """
    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_write_access(me)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)

    task = await get_task(task_id)
    if task is None or task.get("project_id") != project["id"]:
        raise _rejected("任务不存在", status=404)
    if project["status"] != svc.ACTIVE:
        raise _rejected("项目已经归档", status=409)
    task["untrusted"] = bool(task["untrusted"] or task.get("created_by_uid") != caller.uid)
    if task["status"] == svc.TASK_ACTIVE and task.get("claimed_by_uid") == caller.uid:
        return {"status": svc.TASK_ACTIVE, "untrusted": task["untrusted"], "task": task}
    try:
        svc.can_confirm(task, caller.uid)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)

    if not await confirm_task(task_id=task_id, uid=caller.uid):
        raise _rejected("任务状态已变化，请刷新后重试", status=409)

    logger.info(
        f"项目任务开工：{code}#{task['seq']}（{caller.username or caller.uid}，"
        f"来源 {task['source_kind']}，不可信={task['untrusted']}）"
    )
    # untrusted 原样传回去：客户端要把它带进私聊上下文，
    # **不能因为「这是主人自己的会话」就清掉**。
    return {"status": svc.TASK_ACTIVE, "untrusted": task["untrusted"], "task": task}


@router.put("/{code}")
async def update_project(code: str, response: Response,
                         name: str = Body(..., embed=True),
                         description: str = Body("", embed=True),
                         caller: Caller = Depends(require_caller)):
    from db import init_async_session
    from model.database import Project, now
    from sqlalchemy import update
    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)
    if not name.strip() or len(name.strip()) > 255 or len(description) > 1024:
        raise _rejected("请填写项目名称（最多 255 字），描述最多 1024 字")
    factory = await init_async_session()
    async with factory() as session:
        await session.execute(update(Project).where(Project.id == project["id"]).values(
            name=name.strip(), description=description.strip(), updated_at=now()))
        await session.commit()
    return {"name": name.strip(), "description": description.strip()}


@router.post("/{code}/restore")
async def restore_project(code: str, response: Response, caller: Caller = Depends(require_caller)):
    from db import init_async_session
    from model.database import Project, now
    from sqlalchemy import update
    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)
    factory = await init_async_session()
    async with factory() as session:
        await session.execute(update(Project).where(Project.id == project["id"]).values(status=svc.ACTIVE, updated_at=now()))
        await session.commit()
    return {"status": svc.ACTIVE}


@router.post("/{code}/transfer")
async def transfer_project(code: str, response: Response,
                           member_id: int = Body(..., embed=True),
                           caller: Caller = Depends(require_caller)):
    from db import init_async_session
    from model.database import Project, ProjectMember, now
    from sqlalchemy import select
    _no_store(response)
    project, me = await _resolve(code, caller)
    try:
        svc.require_owner(me, is_admin=caller.role in _ADMIN_ROLES)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=403)
    factory = await init_async_session()
    async with factory() as session:
        row = (await session.execute(select(Project).where(Project.id == project["id"]).with_for_update())).scalar_one()
        members = (await session.execute(select(ProjectMember).where(
            ProjectMember.project_id == row.id, ProjectMember.status == svc.ACTIVE,
            ProjectMember.member_kind != svc.KIND_AGENT))).scalars().all()
        if caller.role not in _ADMIN_ROLES and not any(m.user_uid == caller.uid and m.role == svc.ROLE_OWNER for m in members):
            raise _rejected("只有负责人可以移交项目", status=403)
        target = next((m for m in members if m.id == member_id), None)
        if target is None:
            raise _rejected("请选择当前项目中的人成员", status=404)
        for member in members:
            if member.user_uid == caller.uid and member.id != member_id:
                member.role = svc.ROLE_MEMBER
                member.updated_at = now()
        target.role = svc.ROLE_OWNER
        target.updated_at = now()
        row.owner_uid = target.user_uid
        row.updated_at = now()
        await session.commit()
    return {"transferred": True}


@router.post("/{code}/tasks/{task_id}/result")
async def submit_task_result(code: str, task_id: int, response: Response,
                             content: str = Body(..., embed=True),
                             caller: Caller = Depends(require_caller)):
    _no_store(response)
    project, me = await _resolve(code, caller)
    task = await get_task(task_id)
    if not task or task["project_id"] != project["id"]:
        raise _rejected("任务不存在", status=404)
    try:
        svc.require_write_access(me)
        body = svc.validate_message(f"任务 #{task['seq']} · {task['title']}\n\n{content.strip()}")
        if not content.strip():
            raise svc.ProjectRejected("结果不能为空")
        redacted = await svc.redact_for_dept(body, caller.dept_id)
        result = await post_message(project_id=project["id"], sender_kind=svc.KIND_USER,
            sender_uid=caller.uid, sender_username=caller.username, sender_agent_label="",
            dept_id=caller.dept_id, dept_code=caller.dept_code,
            content=redacted.text, content_original=body if redacted.changed else None,
            redaction_hits=redacted.hits, mentions=[], expected_task_id=task_id)
    except svc.ProjectRejected as exc:
        raise _rejected(str(exc), status=409)
    return {"status": "submitted", "message": result}


@router.post("/{code}/tasks/{task_id}/accept")
async def accept_task_result(code: str, task_id: int, response: Response,
                             caller: Caller = Depends(require_caller)):
    from db import init_async_session
    from model.database import Project, ProjectMember, ProjectTask, now
    from sqlalchemy import select
    _no_store(response)
    project, _ = await _resolve(code, caller)
    factory = await init_async_session()
    async with factory() as session:
        row = (await session.execute(select(Project).where(Project.id == project["id"]).with_for_update())).scalar_one()
        task = (await session.execute(select(ProjectTask).where(ProjectTask.id == task_id, ProjectTask.project_id == row.id).with_for_update())).scalar_one_or_none()
        me = (await session.execute(select(ProjectMember).where(ProjectMember.project_id == row.id,
            ProjectMember.user_uid == caller.uid, ProjectMember.member_kind == svc.KIND_USER,
            ProjectMember.status == svc.ACTIVE))).scalar_one_or_none()
        if row.status != svc.ACTIVE or not task or not me or me.role == "viewer":
            raise _rejected("当前不能验收该任务", status=403)
        if caller.role not in _ADMIN_ROLES and me.role != svc.ROLE_OWNER and task.created_by_uid != caller.uid:
            raise _rejected("只有任务创建人或项目负责人可以验收", status=403)
        if task.status == "done":
            return {"status": "done"}
        if task.status != "submitted" or not task.result_message_id:
            raise _rejected("请先提交结果再验收", status=409)
        task.status = "done"
        task.done_at = now()
        task.updated_at = now()
        await session.commit()
    return {"status": "done"}


@router.get("/{code}/tasks/{task_id}/result")
async def read_task_result(code: str, task_id: int, response: Response,
                           caller: Caller = Depends(require_caller)):
    _no_store(response)
    project, _ = await _resolve(code, caller)
    task = await get_task(task_id)
    if not task or task["project_id"] != project["id"] or not task.get("result_message_id"):
        raise _rejected("任务结果不存在", status=404)
    message = await get_message(project["id"], task["result_message_id"])
    if not message:
        raise _rejected("任务结果不存在", status=404)
    return {"message": message}
