"""项目协作的 sidecar 通路 —— 阶段 27-B

渲染进程 --HTTP--> sidecar（这一层）--HTTPS + APIKey--> 审计网关。

## 为什么必须由 sidecar 代取

`LLANO_GATEWAY_API_KEY` 注入在主进程给 sidecar 的环境里，**渲染进程拿不到也不该
拿到**。所以项目、群聊消息、任务这些住在网关上的数据都要走这一层，而不是让
渲染进程自己去请求网关。

## 独立文件，不进 skills.py

与 llano_skill_space 同一条理由：上游没有「项目协作」这件事，塞进上游文件只会
让每次月度同步都在那儿产生冲突。

## 读与写的失败处理**相反**

- **读**：失败回上一次的缓存并标 `stale`。群聊列表拉不出来不该白屏，更不该把
  已经显示的内容清空 —— 那看起来像「消息没了」。
- **写**：失败抛 4xx/5xx，把网关那句话原样带上。用户按了发送却什么都没发生，
  必须说清楚为什么；**绝不能乐观地伪装成已发送**。

## 轮询有终止条件

P0 的实时通路是 `after_seq` 增量拉取（阶段 27 实施决议 1）。`/sync` 会明确回一个
`stopped` ——退出登录、项目归档、成员被移除、账号停用都落在这里。没有它就会留下
一个对着自己已经没资格看的项目每秒敲一次网关的后台死循环。

## 认领与确认是两个接口，这里也不合并

`claim` 返回体里的 `confirmation` 带着任务原文，界面必须停下来呈给人看，拿到人的
确认再调 `confirm`。合并成一步就把 P0-6 那道人在环闸门抹掉了 —— 而群里发一个夹带
注入的任务描述、诱导 agent 认领、回私聊执行，正是那一下要挡的东西。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from tools.project_sync import ProjectSync, next_delay

_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

# 恢复水位、去重与 epoch 的唯一持有者。进程内单例：sidecar 一个进程服务一个账号，
# 而换了账号靠 note_identity 自己清场，不靠界面记得调退出接口 ——
# 界面忘了调的那一次，正是账号已经换了的那一次。
_sync = ProjectSync()

# 读缓存：失败时回上一次的内容并标 stale。只存**已经脱敏过**的网关响应，
# 不存原文（原文只走审计侧，读接口根本拿不到）。
_cache: Dict[str, Any] = {}
_cache_at: Dict[str, float] = {}
# 每个项目连续失败/空转了几轮，只用来算退避。
_error_rounds: Dict[str, int] = {}
_idle_rounds: Dict[str, int] = {}

# 网关说「你没资格」的那几个码。它们是终止条件，不是重试信号。
_REVOKED_STATUSES = frozenset({401, 403, 404})


def _identity() -> str:
    from tools import llano_projects

    return llano_projects.identity_fingerprint()


def _clear_group_sessions() -> None:
    from tools import llano_projects

    llano_projects.clear_group_sessions()


def _sync_identity() -> tuple[str, bool]:
    """对齐身份，返回 (指纹, 是否刚清过场)。

    身份变了要连缓存一起清 —— 只清水位不清缓存的话，新账号会先看到上一个账号的
    群聊内容，然后才被真实数据覆盖。那一瞬间的内容泄漏是真的泄漏。
    """
    identity = _identity()
    changed = _sync.note_identity(identity)
    if changed:
        _cache.clear()
        _cache_at.clear()
        _error_rounds.clear()
        _idle_rounds.clear()
        # 缓存的群聊令牌也归上一个身份。它按指纹分键，本来就落不到新身份上，
        # 这里显式清一次是为了不把上一个人的凭据留在内存里等过期。
        _clear_group_sessions()
    return identity, changed


def _cached(key: str) -> Optional[Any]:
    return _cache.get(key)


def _remember(key: str, value: Any) -> None:
    _cache[key] = value
    _cache_at[key] = time.time()


async def _read(key: str, fn, *, empty):
    """读路径的统一形状：拿到就更新缓存，拿不到就回缓存并标 stale。

    成功的空列表也是最新事实。只有读取失败才保留缓存；失权立即清除缓存。
    """
    from tools.llano_skill_hub import SkillHubUnavailable
    identity = _identity()
    try:
        value = await asyncio.to_thread(fn)
    except SkillHubUnavailable as exc:
        if getattr(exc, "status", None) in _REVOKED_STATUSES:
            _cache.pop(key, None)
            raise HTTPException(status_code=int(exc.status), detail=str(exc)) from exc
        return {"ok": False, "stale": True, "data": _cached(key) or empty}
    if identity != _identity():
        raise HTTPException(status_code=409, detail="账号已切换，请刷新")
    _remember(key, value)
    return {"ok": True, "stale": False, "data": value if value is not None else empty}


# ==================== 读：失败保留缓存 ====================


@router.get("/api/projects")
async def list_projects():
    from tools import llano_projects

    identity, changed = _sync_identity()
    if not identity:
        # 没登录不是错误，是「这个页面现在没有内容」。回 200 让界面显示登录提示，
        # 而不是弹一个「加载失败」。
        return {"projects": [], "authenticated": False, "stale": False,
                "identity_changed": changed}

    result = await _read("projects", llano_projects.list_projects, empty=[])
    if not result["stale"]:
        for project in result["data"]:
            if project.get("status", "active") == "active":
                _sync.resume(project["code"])
    return {"projects": result["data"], "authenticated": True,
            "stale": result["stale"], "identity_changed": changed}


@router.get("/api/projects/{code}/members")
async def list_members(code: str):
    from tools import llano_projects

    _sync_identity()
    result = await _read(f"members:{code}", lambda: llano_projects.list_members(code), empty=[])
    return {"members": result["data"], "stale": result["stale"]}


@router.get("/api/projects/{code}/tasks")
async def list_tasks(code: str, status: str = Query("")):
    from tools import llano_projects

    _sync_identity()
    key = f"tasks:{code}:{status}"
    result = await _read(key, lambda: llano_projects.list_tasks(code, status=status), empty=[])
    return {"tasks": result["data"], "stale": result["stale"]}


@router.get("/api/projects/{code}/messages")
async def list_messages(code: str, after_seq: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=200)):
    """历史消息。**纯读，不动恢复水位。**

    与 `/sync` 是两件事，分开是因为它们回答的问题不同：

    - `/sync`：「从我上次看到的地方往后，有什么新的」—— 有状态，会推进水位
    - 这一条：「这个项目现在有哪些消息」—— 无状态，谁问都给同一份

    只有 `/sync` 的话，页面一重开就是空的：水位在 sidecar 进程里，已经被推到
    最后，而渲染进程那份内存随刷新没了 —— 表现为「聊天记录不见了，等有人再说
    一句才冒出来」。真机上就是这么撞见的。
    """
    from tools import llano_projects

    _sync_identity()
    key = f"messages:{code}:{after_seq}"
    result = await _read(
        key, lambda: llano_projects.messages_since(code, after_seq=after_seq, limit=limit), empty=[]
    )
    return {"messages": result["data"], "stale": result["stale"]}


@router.get("/api/projects/{code}/sync")
async def sync_messages(
    code: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=200),
):
    """增量拉取。**只回这一轮新出现的消息**，重复的在这里就被去重掉了。

    `after_seq` 由客户端带上，但服务端水位取两者较大的那个：客户端把游标往回调
    只会让它把已经显示过的消息再拉一遍（水位只前进，见 tools/project_sync）。

    响应里的 `stopped` 是轮询的终止条件，`retry_after` 是下一轮该等多久 ——
    退避算在这里而不是界面里，是因为界面上有三个地方会触发拉取，各写一份退避
    就会各自漂移。
    """
    from tools import llano_projects
    from tools.llano_skill_hub import SkillHubUnavailable

    identity, changed = _sync_identity()
    if not identity:
        return {"messages": [], "next_seq": 0, "stopped": True,
                "reason": "signed-out", "identity_changed": changed}

    if _sync.is_revoked(code):
        return {"messages": [], "next_seq": 0, "stopped": True,
                "reason": _sync.revoked_reason(code)}

    cursor = _sync.cursor(code)
    # 客户端游标只能往前推水位，不能把它拉回去。
    cursor.seq = max(cursor.seq, int(after_seq or 0))
    epoch = _sync.epoch
    start_seq = cursor.seq

    try:
        batch = await asyncio.to_thread(
            llano_projects.messages_since, code, after_seq=start_seq, limit=limit
        )
    except SkillHubUnavailable as exc:
        status = getattr(exc, "status", None)
        if status in _REVOKED_STATUSES:
            # 项目归档、被移出成员、账号停用。重试没有意义，停。
            _sync.forget(code, reason="revoked")
            _log.info("协作：停止轮询 %s（网关 %s：%s）", code, status, exc)
            return {"messages": [], "next_seq": start_seq, "stopped": True,
                    "reason": "revoked", "detail": str(exc)}

        rounds = _error_rounds.get(code, 0) + 1
        _error_rounds[code] = rounds
        # 抖动：保留水位与已有内容，退避后再来。
        return {"messages": [], "next_seq": start_seq, "stopped": False,
                "offline": True, "detail": str(exc),
                "retry_after": next_delay(error_rounds=rounds)}

    _error_rounds.pop(code, None)
    fresh, next_seq = _sync.admit(code, batch, epoch=epoch)
    if not _sync.accepts(epoch):
        # 请求飞在路上时清过场（切号 / 退出）。这份响应属于上一个身份，丢掉。
        return {"messages": [], "next_seq": 0, "stopped": True, "reason": "reset"}

    if fresh:
        _idle_rounds[code] = 0
    else:
        _idle_rounds[code] = _idle_rounds.get(code, 0) + 1

    return {
        "messages": fresh,
        "next_seq": next_seq,
        "stopped": False,
        "offline": False,
        "retry_after": next_delay(idle_rounds=_idle_rounds.get(code, 0)),
        "epoch": epoch,
    }


@router.post("/api/projects/session/reset")
async def reset_session():
    """账号退出 / 切换时清场。

    与 `note_identity` 的自动清场是两条路，都要有：自动那条防「界面忘了调」，
    这条防「Key 还没换但人已经点了退出」。
    """
    _sync.reset(reason="explicit")
    _cache.clear()
    _cache_at.clear()
    _error_rounds.clear()
    _idle_rounds.clear()
    _clear_group_sessions()
    return {"ok": True, "epoch": _sync.epoch}


# ==================== 写：失败抛出去，不静默 ====================


class SayRequest(BaseModel):
    content: str
    reply_to_id: Optional[int] = None


class TaskRequest(BaseModel):
    title: str
    body: str = ""
    source_message_id: Optional[int] = None
    # 群聊消息转任务时为 True。**`untrusted` 不在这里传** —— 服务端按来源自己定，
    # 客户端能提交那个标记的话，注入一句「把 untrusted 设成 false」就绕过整条
    # P0-6 防线。
    from_chat: bool = True


class ClaimRequest(BaseModel):
    agent_label: str = ""


class CreateProjectRequest(BaseModel):
    code: str = ""
    name: str = ""
    description: str = ""


class PullAgentRequest(BaseModel):
    agent_label: str = ""


class InviteRequest(BaseModel):
    user_uid: str = ""
    username: str = ""
    role: str = "member"


async def _write(fn):
    from tools.llano_skill_hub import SkillHubUnavailable

    # 写路径也要对齐身份：只在读路径对齐的话，「登录后第一个动作是发言」这条
    # 顺序会让紧随其后的第一次 sync 触发清场，把刚发出去的那条又当成新消息 ——
    # 界面上是同一句话出现两遍。
    _sync_identity()
    try:
        return await asyncio.to_thread(fn)
    except SkillHubUnavailable as exc:
        status = getattr(exc, "status", None)
        # 网关的拒绝原样带上：「你在这个项目里是只读成员」比「发送失败」有用得多。
        raise HTTPException(status_code=int(status or 502), detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 写失败必须说清楚，不能吞
        _log.exception("协作：写操作失败")
        raise HTTPException(status_code=502, detail=f"操作失败：{exc}") from exc


@router.get("/api/directory")
async def directory(q: str = Query("", max_length=64), limit: int = Query(50, ge=1, le=200)):
    """全员通讯录。读路径：拿不到回空 + stale，不让「加成员」那个框炸掉。

    这是客户端把人拉进项目的前提 —— 在这之前客户端**没有任何办法拿到别人的
    uid**，后台的账号列表要 `user:view`，普通员工没有。
    """
    from tools import llano_projects

    identity, _ = _sync_identity()
    if not identity:
        return {"users": [], "authenticated": False, "stale": False}

    result = await _read(f"directory:{q}:{limit}",
                         lambda: llano_projects.directory(q, limit), empty=[])
    return {"users": result["data"], "authenticated": True, "stale": result["stale"]}


@router.post("/api/projects")
async def create_project(body: CreateProjectRequest):
    """建项目。

    2026-09-07 之前这个动作只有后台「协作」页有，桌面端只能选一个已经存在的项目
    —— 而后台的定位是看板。协作发生在客户端，建项目当然也该在客户端。

    `code` 的规则（小写、不可改）由服务端判，这里只挡空值：把同一条规则抄一份在
    客户端，两份迟早会漂，而漂的那天用户看到的是「客户端说可以，服务端说不行」。
    """
    from tools import llano_projects

    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="项目标识不能为空")

    return await _write(lambda: llano_projects.create_project(
        code, (body.name or "").strip() or code, (body.description or "").strip(),
    ))


@router.post("/api/projects/{code}/repository/retry")
async def retry_repository(code: str):
    """显式重试，失败必须透传；不能让界面假装仓库已经可用。"""
    from tools import llano_projects

    return await _write(lambda: llano_projects.retry_repository(code))


@router.post("/api/projects/{code}/members")
async def invite_member(code: str, body: InviteRequest):
    """把一个人加进项目。**只有负责人能加**，判定在服务端 —— 这里不预判，
    让网关那句「只有项目负责人能加成员」原样传到界面上。"""
    from tools import llano_projects

    uid = (body.user_uid or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="要加的成员不能为空")

    return await _write(lambda: llano_projects.invite_member(
        code, uid, (body.username or "").strip(), (body.role or "member").strip(),
    ))


@router.post("/api/projects/{code}/agents")
async def pull_my_agent(code: str, body: PullAgentRequest):
    """把自己的 agent 拉进项目。**只能拉自己的**，见 llano_projects.pull_my_agent。"""
    from tools import llano_projects

    return await _write(
        lambda: llano_projects.pull_my_agent(code, (body.agent_label or "").strip())
    )


@router.post("/api/projects/{code}/messages")
async def say(code: str, body: SayRequest):
    """发言。脱敏在**服务端**做，这里不做也不该做（P0-2）。

    返回体里网关会告诉我们这条有没有被遮（`redaction_hits`）。界面要显示出来：
    收到的人有权知道自己看到的不是原话，发的人也有权知道自己的话被改了。
    """
    from tools import llano_projects

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息正文不能为空")

    # ⚠️ **不要**把这条写进游标。
    #
    # 写回响应里只有 {id, seq, redacted, mentions} —— 没有正文，也没有发送者：
    # 投递出去的那份是**服务端脱敏后**的文本，只有服务端知道。把它塞进去重集合
    # 等于让下一轮 sync 跳过这条消息，于是自己发的话在界面上永远不出现（真机
    # 上就是这么表现的：列表里多了一条空记录，刷新之后才好）。
    #
    # 正确的形状是：发送只负责发送，消息统一由 sync 带回来 —— 那份才是别人
    # 看到的那份。
    return await _write(
        lambda: llano_projects.say(code, content, reply_to_id=body.reply_to_id)
    )


@router.post("/api/projects/{code}/tasks")
async def create_task(code: str, body: TaskRequest):
    """建任务，也是「群聊消息转任务」的落点（带 `source_message_id`）。"""
    from tools import llano_projects

    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="任务标题不能为空")

    return await _write(lambda: llano_projects.create_task(
        code, title, body.body or "",
        source_message_id=body.source_message_id,
        from_chat=bool(body.from_chat),
    ))


@router.post("/api/projects/{code}/tasks/{task_id}/claim")
async def claim_task(code: str, task_id: int, body: ClaimRequest | None = None):
    """认领。**只到 claimed，不到 active。**

    返回体里的 `confirmation` 带着任务原文（原样，不摘要不高亮），界面必须停下来
    呈给人看。这里刻意不提供「认领并开工」的便利接口：便利接口会被用，然后 P0-6
    那道防线就没了。
    """
    from tools import llano_projects

    label = (body.agent_label if body else "") or ""
    return await _write(lambda: llano_projects.claim(code, task_id, agent_label=label))


@router.post("/api/projects/{code}/tasks/{task_id}/confirm")
async def confirm_task(code: str, task_id: int):
    """人看过原文之后确认开工。

    返回体里的 `untrusted` 要**跟着任务描述一起进入私聊上下文**，不能因为
    「这是主人自己的会话」就清掉。
    """
    from tools import llano_projects

    return await _write(lambda: llano_projects.confirm(code, task_id))


# ==================== 群聊 agent 的一轮执行（阶段 27-E）====================


class RespondToRequest(BaseModel):
    respond_to: str
    allowlist: Optional[List[str]] = None


@router.put("/api/projects/{code}/members/{member_id}/respond-to")
async def set_respond_to(code: str, member_id: int, body: RespondToRequest):
    """改这个 agent 接受谁的指令。

    只是配置入口 —— **判定在服务端**，桌面端这边改不动任何一条实际权限。
    非法值由网关拒绝（不静默纠正成默认值），错误原样抛给界面。
    """
    from tools import llano_projects

    return await _write(
        lambda: llano_projects.set_respond_to(
            code, member_id, body.respond_to, allowlist=body.allowlist or []
        )
    )


@router.get("/api/projects/{code}/turns")
async def list_turns(code: str):
    """agent 活跃状态。读路径：失败降级成空，不让页面崩。"""
    from tools import llano_projects

    _sync_identity()
    result = await _read(f"turns:{code}", lambda: llano_projects.list_turns(code), empty={})
    data = result["data"] or {}
    return {"pending": data.get("pending", []), "availability": data.get("availability", []),
            "turns": data.get("turns", []), "hops": data.get("hops", 0),
            "hop_limit": data.get("hop_limit", 8), "stale": result["stale"]}


class TurnTickRequest(BaseModel):
    after_seq: int = 0
    reason: str = "mention"
    model: str = ""


@router.post("/api/projects/{code}/agents/{member_id}/tick")
async def run_agent_turn(code: str, member_id: int, body: TurnTickRequest | None = None):
    """让**我自己的** agent 在这个项目里跑一轮（如果有该回的事）。

    ## 为什么是「桌面来敲一下」而不是后台常驻循环

    由应用级接收服务发起，页面切换不影响接收。主动暂停、账号退出和项目失权
    会停止后续检查；网关租约仍负责跨窗口去重与权限。

    ## 判定全在服务端

    有没有该回的事、谁触发的、有没有权限、跳数到没到、这个项目现在能不能开工，
    都由网关从库里推。这里只负责「跑」，跑不跑得成不由它说了算 —— 所以
    `ran=false` 是正常返回，不是错误。
    """
    from tools import llano_projects
    from tools.collab_agent_turn import gateway_completion, run_turn
    from tools.llano_skill_hub import hub_origin

    _sync_identity()
    payload = body or TurnTickRequest()

    def _run():
        model = (payload.model or "").strip() or _default_group_model()

        return run_turn(
            code=code,
            member_id=member_id,
            after_seq=payload.after_seq,
            reason=payload.reason,
            complete=gateway_completion(hub_origin(), model, max_tokens=_group_output_budget()),
        )

    return await _write(_run)


def _group_output_budget() -> int:
    from hermes_cli.config import load_config
    config = load_config() or {}
    value = (config.get("collaboration") or {}).get("max_output_tokens", 4096)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("collaboration.max_output_tokens 必须为正整数")
    return value


def _default_group_model() -> str:
    """群聊回复用哪个模型。

    读的是本机 profile 里已经配好的那个 —— 群聊不该有一套独立的模型配置：
    两套配置意味着「我明明换了模型，群里那个还是老的」，而且没有任何一处会说
    为什么。取不到时给一个空串，让网关按它自己的默认路由决定。
    """
    try:
        from hermes_cli.config import load_config  # type: ignore

        config = load_config() or {}
        # 配置里这一项叫 `model.default`（不是 name）—— 与 CLI 用的是同一处。
        model = ((config.get("model") or {}) if isinstance(config, dict) else {}).get("default")

        return str(model or "")
    except Exception:  # noqa: BLE001 — 取不到模型名不该让这一轮直接失败
        return ""


def _reset_for_tests() -> None:  # pragma: no cover - 测试夹具用
    _sync.reset(reason="test")
    _cache.clear()
    _cache_at.clear()
    _error_rounds.clear()
    _idle_rounds.clear()
    _clear_group_sessions()


class ProjectDetailsRequest(BaseModel):
    name: str
    description: str = ""


class TransferRequest(BaseModel):
    member_id: int


@router.put("/api/projects/{code}")
async def update_project(code: str, body: ProjectDetailsRequest):
    from tools import llano_projects
    return await _write(lambda: llano_projects.update_project(code, body.name, body.description))


@router.delete("/api/projects/{code}/members/{member_id}")
async def remove_project_member(code: str, member_id: int):
    from tools import llano_projects
    result = await _write(lambda: llano_projects.remove_member(code, member_id))
    _cache.clear()
    return result


@router.post("/api/projects/{code}/archive")
async def archive_project(code: str):
    from tools import llano_projects
    result = await _write(lambda: llano_projects.archive(code))
    _cache.clear()
    return result


@router.post("/api/projects/{code}/restore")
async def restore_project(code: str):
    from tools import llano_projects
    result = await _write(lambda: llano_projects.restore(code))
    _cache.clear()
    return result


@router.post("/api/projects/{code}/transfer")
async def transfer_project(code: str, body: TransferRequest):
    from tools import llano_projects
    return await _write(lambda: llano_projects.transfer(code, body.member_id))


@router.post("/api/projects/{code}/agents/{member_id}/presence")
async def agent_presence(code: str, member_id: int):
    from tools import llano_projects
    return await _write(lambda: llano_projects.presence(code, member_id))


class FinishTurnRequest(BaseModel):
    status: str = "cancelled"


@router.post("/api/projects/{code}/turns/{turn_id}/finish")
async def finish_turn(code: str, turn_id: int, body: FinishTurnRequest):
    from tools import llano_projects
    if body.status != "cancelled":
        raise HTTPException(status_code=400, detail="此入口仅用于停止回复")
    return await _write(lambda: llano_projects.turn_finish(code, turn_id, status="cancelled"))


class TaskResultRequest(BaseModel):
    content: str


@router.post("/api/projects/{code}/tasks/{task_id}/result")
async def submit_task_result(code: str, task_id: int, body: TaskResultRequest):
    from tools import llano_projects
    return await _write(lambda: llano_projects.submit_task_result(code, task_id, body.content))


@router.post("/api/projects/{code}/tasks/{task_id}/accept")
async def accept_task_result(code: str, task_id: int):
    from tools import llano_projects
    return await _write(lambda: llano_projects.accept_task_result(code, task_id))


@router.get("/api/projects/{code}/tasks/{task_id}/result")
async def get_task_result(code: str, task_id: int):
    from tools import llano_projects
    return await _write(lambda: llano_projects.get_task_result(code, task_id))
