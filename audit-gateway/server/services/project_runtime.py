"""群聊 agent 运行时的判定层 —— 阶段 27-A（契约先行）

这一层不碰数据库、不发请求，只把**语义**钉死：谁可以触发一个 agent、什么事件
算硬触发、一个 turn 的状态怎么走、连续 agent 对话在第几跳停、写回的身份从哪里
来。全是纯函数，因为这些判断出错的代价远高于它们的实现难度。

## 为什么执行域必须在服务端

每个 agent 跑在**它主人自己的机器上**（桌面 sidecar）。所以「同一个项目同时最多
一个 in-flight turn」和「连续 agent-to-agent 最多 8 跳」这两条是**跨机器**约束：
A 的 sidecar 完全不知道 B 的 sidecar 正在回同一个项目。放在客户端做的话，两台机器
同时开工就是必然，而不是偶发。

所以服务端发**turn 租约**：客户端拿不到租约就不许开工，租约里锁死 project、agent、
触发事件与 `trigger_request_id`。这同时解决了写回身份 —— 见 `writeback_identity`。

## 客户端提交的字段一律不作数

与决策 #45 同一条：身份绝不能由 agent 提供。这里对应的是
`sender_kind` / `owner_uid` / `mentions` / `untrusted` / `hops` 全部由服务端从
租约和成员表推导；载荷里出现同名字段时忽略，而不是「以载荷为准」。
`writeback_identity` 是唯一的身份出口，绕过它自己拼 sender 字段就等于把这条作废。

## 失败关闭

认不出来的 `respond_to`、认不出来的触发原因、过期或跨项目的租约，一律拒绝。
一个拼错的枚举值不该换来「所有人都能指挥这个 agent」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from services.project import ACTIVE, KIND_AGENT, KIND_USER, ProjectRejected

# ==================== 谁可以触发（P0-D）====================

RESPOND_OWNER_ONLY = "owner_only"
RESPOND_ALLOWLIST = "allowlist"
RESPOND_PROJECT_MEMBERS = "project_members"
RESPOND_NOBODY = "nobody"

RESPOND_TO_VALUES = frozenset({
    RESPOND_OWNER_ONLY, RESPOND_ALLOWLIST, RESPOND_PROJECT_MEMBERS, RESPOND_NOBODY,
})

# 默认值是 project_members：它只允许成员触发一个**无能力的群聊回复**，
# 不授予 MCP、skill、个人记忆或主人凭据。默认更严的话，群聊里 @ 一个 agent
# 不回复会被当成故障，而人的应对方式是把所有 agent 都调到最松。
DEFAULT_RESPOND_TO = RESPOND_PROJECT_MEMBERS


def normalize_respond_to(value: Any) -> str:
    """配置路径：主人在项目页里改这个值时用。非法值**拒绝**，不静默纠正。

    静默纠正成默认值等于「你以为设了 owner_only，其实是 project_members」——
    权限设置上的静默纠正一律不可接受。
    """
    if value is None or value == "":
        return DEFAULT_RESPOND_TO
    if not isinstance(value, str) or value not in RESPOND_TO_VALUES:
        raise ProjectRejected(
            "触发范围只能是 owner_only / allowlist / project_members / nobody"
        )
    return value


def effective_respond_to(agent_member: Optional[Dict[str, Any]]) -> str:
    """读取路径：库里那一列。认不出来的值当 `nobody`（失败关闭）。

    与配置路径的处理刻意相反：配置时报错让人改，运行时报错只会让 agent 在群里
    抛异常，所以运行时选择「谁都不许触发」——最坏结果是它不说话，不是它听所有人的。
    """
    if not agent_member:
        return RESPOND_NOBODY
    value = agent_member.get("respond_to")
    if value is None or value == "":
        # 迁移前建的行：按当时的语义，也就是默认值。
        return DEFAULT_RESPOND_TO
    return value if value in RESPOND_TO_VALUES else RESPOND_NOBODY


def may_trigger(
    agent_member: Optional[Dict[str, Any]],
    requester: Optional[Dict[str, Any]],
    *,
    allowlist: Iterable[str] = (),
) -> bool:
    """`requester`（服务端查出来的项目成员行）能不能指挥 `agent_member`。

    两侧都必须是**服务端查出来的成员行**。传客户端载荷进来就等于让请求方自称
    成员，这个函数的返回值也就没有意义了。
    """
    if not agent_member or agent_member.get("member_kind") != KIND_AGENT:
        return False
    if agent_member.get("status") != ACTIVE:
        return False
    # 项目不同就不同 —— 跨项目拿一个成员行来判定是最容易漏的一处越权。
    if not requester or requester.get("status") != ACTIVE:
        return False
    if requester.get("member_kind") != KIND_USER:
        # agent 之间的连续对话由跳数熔断管，触发权限只认人。
        # 让 agent 出现在这里等于给「A 的 agent 指挥 B 的 agent」开一条免检通道。
        return False
    if agent_member.get("project_id") != requester.get("project_id"):
        return False

    mode = effective_respond_to(agent_member)
    owner_uid = agent_member.get("user_uid")
    requester_uid = requester.get("user_uid")

    if mode == RESPOND_NOBODY:
        return False
    if mode == RESPOND_OWNER_ONLY:
        return bool(owner_uid) and requester_uid == owner_uid
    if mode == RESPOND_ALLOWLIST:
        if bool(owner_uid) and requester_uid == owner_uid:
            return True
        # 白名单存在服务端，且只在**本项目**内有意义。
        return requester_uid in {str(uid) for uid in allowlist if uid}
    # project_members：到这里 requester 已经被证明是本项目的活跃人成员。
    return True


# ==================== 什么算硬触发（P0-B）====================

TRIGGER_MENTION = "mention"                # 正文按服务端成员名单解析出明确 @agent
TRIGGER_TASK_ASSIGNED = "task_assigned"    # 任务明确分配给该 agent
TRIGGER_TASK_EVENT = "task_event"          # 已认领任务的确认/追问/结果提交
TRIGGER_OWNER_REQUEST = "owner_request"    # 主人在项目页显式要求自己的 agent 发言

TRIGGER_REASONS = frozenset({
    TRIGGER_MENTION, TRIGGER_TASK_ASSIGNED, TRIGGER_TASK_EVENT, TRIGGER_OWNER_REQUEST,
})


@dataclass(frozen=True)
class TriggerEvent:
    """一件**已经由服务端证明过**的触发事件。

    `event_id` 用 `project_id:seq`，不用全局自增 id：项目 A 的游标不该因为项目 B
    的写入而跳号，而客户端的去重集合与恢复水位都建在这个 id 上。
    """

    project_id: int
    seq: int
    reason: str
    agent_member_id: int
    requester_uid: str
    message_id: Optional[int] = None
    task_id: Optional[int] = None

    @property
    def event_id(self) -> str:
        return f"{self.project_id}:{self.seq}"


def event_id_of(project_id: Any, seq: Any) -> str:
    return f"{int(project_id)}:{int(seq)}"


def normalize_trigger_reason(value: Any) -> str:
    """P0 只有四种硬触发。别的一律拒绝 —— 包括「看起来像在问我」这种。

    普通消息在 P0 **不产生任何模型调用**；语义路由是 P1 的事，而且要走
    `call_kind=gate` 单独记账。这里放宽一个值，等于把 P1 的成本悄悄提前上线。
    """
    if not isinstance(value, str) or value not in TRIGGER_REASONS:
        raise ProjectRejected("触发原因不在允许的硬触发集合内")
    return value


def should_ignore_event(event_sender: Dict[str, Any], agent_member: Dict[str, Any]) -> bool:
    """agent 不被自己写回的消息触发。

    否则一条回复就是一个新事件，而这个事件又满足触发条件 —— 熔断跳数只能限制它
    刷 8 条，限制不住它一定会刷。
    """
    if event_sender.get("sender_kind") != KIND_AGENT:
        return False
    return (
        event_sender.get("sender_uid") == agent_member.get("user_uid")
        and (event_sender.get("sender_agent_label") or "")
        == (agent_member.get("agent_label") or "")
    )


# ==================== 连续回复熔断（P0-C）====================

# 默认 8 跳。行为配置，不是环境变量：换一台机器部署不该换来一个不同的熔断阈值。
# 客户端可以展示这个数，但服务端不接受客户端提高它。
MAX_AGENT_HOPS = 8


def next_hop_count(previous: int, sender_kind: str) -> int:
    """真实人类成员的新消息把计数清零；agent 的消息加一。

    「人发言才重置」是这条熔断的全部意义：A→B→A 循环里没有人说话，所以它必须
    在有限步内停下。
    """
    if sender_kind == KIND_USER:
        return 0
    return max(0, int(previous or 0)) + 1


def hops_exhausted(hops: int, *, limit: int = MAX_AGENT_HOPS) -> bool:
    return int(hops or 0) >= int(limit)


HOP_LIMIT_NOTICE = (
    "本项目里 agent 之间已经连续对话 {limit} 轮，先停一下。"
    "有需要的话，谁说一句话就能继续。"
)


def hop_limit_notice(*, limit: int = MAX_AGENT_HOPS) -> str:
    """达到上限后发在群里的提示。

    不写「检测到循环」「疑似异常」这类词：绝大多数情况下这只是两个 agent 聊得
    太投入，把它说成故障会让人去点重试。
    """
    return HOP_LIMIT_NOTICE.format(limit=limit)


# ==================== turn 状态机与租约（P0-C）====================

TURN_QUEUED = "queued"
TURN_RUNNING = "running"
TURN_COMPLETED = "completed"
TURN_FAILED = "failed"
TURN_CANCELLED = "cancelled"

TURN_STATES = frozenset({
    TURN_QUEUED, TURN_RUNNING, TURN_COMPLETED, TURN_FAILED, TURN_CANCELLED,
})
# 终态是墓碑：进去之后任何迟到的心跳都不能把它拉回运行中。
TURN_TERMINAL = frozenset({TURN_COMPLETED, TURN_FAILED, TURN_CANCELLED})

# 队列与重试上限。超过就进可见的失败态 —— 静默丢弃和无限重试都不允许，
# 前者让人以为 agent 没看见，后者让人以为一切正常而账单在涨。
MAX_QUEUE_PER_PROJECT = 32
MAX_BATCH_EVENTS = 16
MAX_TURN_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 60
# 租约续不上就算这个 turn 死了（客户端崩溃、断网、关机），项目才能继续。
TURN_LEASE_TTL_SECONDS = 300


def retry_delay(attempt: int) -> int:
    """指数退避，有上限。attempt 从 1 开始。"""
    n = max(1, int(attempt))
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (n - 1)))


def should_retry(attempt: int, *, limit: int = MAX_TURN_ATTEMPTS) -> bool:
    return int(attempt) < int(limit)


def can_enqueue(queued: int, *, limit: int = MAX_QUEUE_PER_PROJECT) -> None:
    if int(queued) >= int(limit):
        raise ProjectRejected(
            f"这个项目排队中的 agent 任务已达 {limit} 条上限，先处理完再继续"
        )


_ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    TURN_QUEUED: frozenset({TURN_RUNNING, TURN_CANCELLED, TURN_FAILED}),
    TURN_RUNNING: frozenset({TURN_COMPLETED, TURN_FAILED, TURN_CANCELLED}),
    TURN_COMPLETED: frozenset(),
    TURN_FAILED: frozenset(),
    TURN_CANCELLED: frozenset(),
}


def can_transition(current: Any, target: Any) -> bool:
    if current not in TURN_STATES or target not in TURN_STATES:
        return False
    return target in _ALLOWED_TRANSITIONS[current]


def require_transition(current: Any, target: Any) -> str:
    if not can_transition(current, target):
        raise ProjectRejected(f"turn 状态不能从 {current} 变成 {target}")
    return target


def accept_status_update(current: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    """迟到的状态更新要不要采纳。**水位只前进。**

    两条规则，缺一条都会让「已完成的 turn 又显示成运行中」重现：

    1. 当前是终态就一律拒绝 —— 完成 tombstone 优先于任何时间戳
    2. 否则按 (seq, updated_at) 比较，旧的不覆盖新的
    """
    if current.get("status") in TURN_TERMINAL:
        return False
    if not can_transition(current.get("status"), incoming.get("status")):
        return False

    cur_seq = int(current.get("status_seq") or 0)
    new_seq = int(incoming.get("status_seq") or 0)
    if new_seq != cur_seq:
        return new_seq > cur_seq

    cur_at = current.get("updated_at")
    new_at = incoming.get("updated_at")
    if cur_at is None or new_at is None:
        # 同 seq 又没有时间戳可比：不动。重复的同序更新没有信息量。
        return False
    return new_at > cur_at


def merge_batch(events: Sequence[TriggerEvent], *, limit: int = MAX_BATCH_EVENTS) -> List[TriggerEvent]:
    """把相邻事件合成一批上下文。

    **合成不等于合并**：每个事件的 id、seq、发送者和触发原因都要原样留着，
    因为 usage 与拒绝日志是按 `trigger_request_id` 逐条对账的。这里只做去重、
    排序和截断，不生成任何「合并后的事件」。
    """
    seen: set = set()
    ordered: List[TriggerEvent] = []
    for event in sorted(events, key=lambda e: (int(e.project_id), int(e.seq))):
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        ordered.append(event)
    # 截断保留**最早**的一批：先来先答，晚到的留在队列里等下一个 turn。
    return ordered[:limit]


# ==================== 写回身份（P0-E）====================


@dataclass(frozen=True)
class WritebackIdentity:
    sender_kind: str
    sender_uid: str
    sender_agent_label: str
    trigger_request_id: str


def require_lease(turn: Optional[Dict[str, Any]], *, project_id: int, uid: str) -> Dict[str, Any]:
    """写回前先证明这把租约是这个项目、这个调用者的，而且还活着。

    跨项目（拿项目 A 的租约往项目 B 写）和跨归属（拿别人的租约冒充别人的 agent）
    是这条路径上仅有的两种越权，所以两条都在这里挡，而不是分散在调用点。
    """
    if not turn:
        raise ProjectRejected("这个 turn 不存在或已经结束")
    if int(turn.get("project_id") or 0) != int(project_id):
        raise ProjectRejected("这个 turn 不属于该项目")
    if turn.get("owner_uid") != uid:
        raise ProjectRejected("这个 turn 不属于你")
    if turn.get("status") != TURN_RUNNING:
        raise ProjectRejected("这个 turn 不在运行中，不能写回")
    return turn


def writeback_identity(turn: Dict[str, Any]) -> WritebackIdentity:
    """agent 发言的身份**只有这一个出口**，且只从租约里取。

    客户端提交的 `sender_kind` / `owner_uid` / `agent_label` 一概不看：能自称
    agent 的接口就是决策 #45 反复说不能做的那种设计。归属挂主人 uid，记账口径
    才接得上（群聊消耗算主人的）。
    """
    return WritebackIdentity(
        sender_kind=KIND_AGENT,
        sender_uid=str(turn["owner_uid"]),
        sender_agent_label=str(turn.get("agent_label") or ""),
        trigger_request_id=str(turn.get("trigger_request_id") or ""),
    )


# ==================== 群聊 turn 的能力边界（决策 #45 客户端那半的契约）====================

# 群聊 turn 允许出现的工具类别。**白名单**，不是黑名单：黑名单会在每次新增工具
# 类别时静默漏一个，而漏掉的那次没有任何报错。
GROUP_TURN_ALLOWED_TOOL_KINDS = frozenset({"chat", "project"})
# 明确点名的禁止项，只为让测试与代码里都能搜到这几个词。
GROUP_TURN_FORBIDDEN_TOOL_KINDS = frozenset({"mcp", "skill", "skill_admin", "credential", "memory"})


def assert_group_toolset(tool_kinds: Iterable[str]) -> None:
    """群聊 turn 的 tool schema 体检。

    群聊只允许讨论、认领/接收任务和提交已有结果。真正要执行时回主人私聊，
    走 P0-6 的原文确认。这条断言存在的意义是：以后新增工具的人**必须**来这里
    显式加类别，而不是默默地让它出现在群聊里。
    """
    extra = {str(k) for k in tool_kinds} - GROUP_TURN_ALLOWED_TOOL_KINDS
    if extra:
        raise ProjectRejected(
            "群聊 agent 不允许持有这些工具：" + "、".join(sorted(extra))
        )
