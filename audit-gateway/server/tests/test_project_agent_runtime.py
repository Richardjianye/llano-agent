"""群聊 agent 运行时的契约与安全用例 —— 阶段 27-A

这一组守的是四条**事后补不了**的东西：

1. **触发权限在服务端** —— 客户端提交的 sender / owner / mention / scope 一律
   不作数；跨项目、跨归属的成员行不能互相授权
2. **P0 只有四种硬触发** —— 普通消息在 P0 不产生任何模型调用，语义路由是 P1
3. **执行域是服务端租约** —— 每个 agent 跑在它主人自己的机器上，单项目单
   in-flight、8 跳熔断、迟到心跳不复活完成的 turn，只能由服务端裁决
4. **写回身份只有一个出口** —— 身份从租约取，不从载荷取（决策 #45）

⚠️ 唯一索引带来的并发互斥（两台机器同时抢租约）没有单元用例：它压在真实的行锁
   上，喂假 session 测出来的绿是假的。与 test_project_collaboration 同一处理，
   留给 e2e。这里测的是**判定**，不是**竞态**。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from services.project import ProjectRejected
from services import project_runtime as rt

OWNER = "uid-owner"
MATE = "uid-mate"
STRANGER = "uid-stranger"


def _agent(**over):
    row = {
        "id": 11,
        "project_id": 1,
        "member_kind": "agent",
        "user_uid": OWNER,
        "agent_label": "小助手",
        "role": "member",
        "status": "active",
        "respond_to": rt.DEFAULT_RESPOND_TO,
    }
    row.update(over)
    return row


def _person(uid=MATE, **over):
    row = {
        "id": 22,
        "project_id": 1,
        "member_kind": "user",
        "user_uid": uid,
        "agent_label": "",
        "role": "member",
        "status": "active",
    }
    row.update(over)
    return row


# ── P0-D 谁可以触发 ────────────────────────────────────────────────────────

def test_default_lets_project_members_trigger():
    """基线。没有这条的话，下面每个「拒绝」都可能只是因为这个函数拒绝一切。"""
    assert rt.may_trigger(_agent(), _person()) is True


def test_owner_only_rejects_other_members():
    assert rt.may_trigger(_agent(respond_to=rt.RESPOND_OWNER_ONLY), _person(MATE)) is False
    assert rt.may_trigger(_agent(respond_to=rt.RESPOND_OWNER_ONLY), _person(OWNER)) is True


def test_allowlist_is_server_side_and_owner_always_in():
    agent = _agent(respond_to=rt.RESPOND_ALLOWLIST)

    assert rt.may_trigger(agent, _person(MATE), allowlist=[MATE]) is True
    assert rt.may_trigger(agent, _person(STRANGER), allowlist=[MATE]) is False
    # 主人不必把自己写进名单：那是每个人都会忘、且忘了就锁死自己 agent 的一步。
    assert rt.may_trigger(agent, _person(OWNER), allowlist=[MATE]) is True


def test_nobody_pauses_without_losing_membership():
    """`nobody` 是暂停接受指令，不是退群 —— 历史与成员关系都还在。"""
    assert rt.may_trigger(_agent(respond_to=rt.RESPOND_NOBODY), _person(OWNER)) is False


def test_unknown_respond_to_fails_closed():
    """认不出来的值当 nobody。

    一个拼错的枚举值不该换来「所有人都能指挥这个 agent」。运行时选择闭嘴，
    最坏结果是它不说话，而不是它听所有人的。
    """
    assert rt.effective_respond_to(_agent(respond_to="everyone")) == rt.RESPOND_NOBODY
    assert rt.may_trigger(_agent(respond_to="everyone"), _person(OWNER)) is False


def test_config_path_rejects_bad_value_instead_of_correcting_it():
    """配置路径与运行时刻意相反：报错让人改，不静默纠正。

    静默纠正等于「你以为设了 owner_only，其实是 project_members」。
    """
    with pytest.raises(ProjectRejected):
        rt.normalize_respond_to("everyone")
    assert rt.normalize_respond_to(None) == rt.DEFAULT_RESPOND_TO


def test_left_member_cannot_trigger_and_left_agent_cannot_be_triggered():
    assert rt.may_trigger(_agent(), _person(status="left")) is False
    assert rt.may_trigger(_agent(status="left"), _person()) is False


def test_cross_project_member_rows_do_not_authorize_each_other():
    """跨项目越权：拿项目 2 的成员身份去指挥项目 1 的 agent。

    两侧都是「真实的活跃成员行」，只有 project_id 不同 —— 所以这条必须在判定
    函数里挡，指望每个调用点自己比对 project_id 是漏一处就破防的写法。
    """
    assert rt.may_trigger(_agent(project_id=1), _person(project_id=2)) is False


def test_agent_cannot_be_the_requester():
    """A 的 agent 直接「指挥」B 的 agent 不算触发权限。

    agent 之间的连续对话由跳数熔断管；在这里给它开口子等于绕过那条熔断。
    """
    assert rt.may_trigger(_agent(), _agent(id=99, user_uid=MATE, agent_label="别人的")) is False


def test_forged_payload_fields_are_not_inputs_here():
    """载荷里自称主人不管用：判定只吃服务端查出来的成员行。"""
    forged = _person(STRANGER)
    forged["owner_uid"] = OWNER          # 客户端瞎写的
    forged["sender_kind"] = "agent"
    forged["respond_to"] = "project_members"

    assert rt.may_trigger(_agent(respond_to=rt.RESPOND_OWNER_ONLY), forged) is False


# ── P0-B 只有四种硬触发 ───────────────────────────────────────────────────

def test_only_the_four_hard_triggers_are_accepted():
    for reason in (rt.TRIGGER_MENTION, rt.TRIGGER_TASK_ASSIGNED,
                   rt.TRIGGER_TASK_EVENT, rt.TRIGGER_OWNER_REQUEST):
        assert rt.normalize_trigger_reason(reason) == reason


@pytest.mark.parametrize("reason", ["", None, "looks_relevant", "gate", "semantic"])
def test_soft_triggers_are_rejected_in_p0(reason):
    """普通消息在 P0 不产生模型调用。

    这里放宽一个值，等于把 P1 的 gate 成本悄悄提前上线，而且是无声的：
    报表上只会看到调用数涨了。
    """
    with pytest.raises(ProjectRejected):
        rt.normalize_trigger_reason(reason)


def test_agent_is_not_triggered_by_its_own_writeback():
    agent = _agent()
    own = {"sender_kind": "agent", "sender_uid": OWNER, "sender_agent_label": "小助手"}
    other = {"sender_kind": "agent", "sender_uid": MATE, "sender_agent_label": "别人的"}
    human = {"sender_kind": "user", "sender_uid": OWNER, "sender_agent_label": ""}

    assert rt.should_ignore_event(own, agent) is True
    assert rt.should_ignore_event(other, agent) is False
    assert rt.should_ignore_event(human, agent) is False


def test_event_id_is_scoped_to_the_project():
    """去重游标用 project_id:seq，不用全局自增 id。

    用全局 id 的话，项目 A 的游标会因为项目 B 的写入而跳号，看起来像丢了消息。
    """
    assert rt.event_id_of(1, 7) == "1:7"
    assert rt.event_id_of(2, 7) != rt.event_id_of(1, 7)


# ── P0-C 熔断与队列 ───────────────────────────────────────────────────────

def test_hop_counter_only_resets_on_a_human_message():
    hops = 0
    for _ in range(3):
        hops = rt.next_hop_count(hops, "agent")
    assert hops == 3
    assert rt.next_hop_count(hops, "user") == 0


def test_agent_loop_stops_at_eight_hops():
    """A→B→A 循环里没有人说话，所以它必须在有限步内停下。"""
    hops = 0
    for _ in range(rt.MAX_AGENT_HOPS):
        assert rt.hops_exhausted(hops) is False
        hops = rt.next_hop_count(hops, "agent")

    assert hops == rt.MAX_AGENT_HOPS
    assert rt.hops_exhausted(hops) is True
    # 人说一句就恢复。
    assert rt.hops_exhausted(rt.next_hop_count(hops, "user")) is False


def test_hop_limit_notice_is_not_alarming():
    notice = rt.hop_limit_notice()
    assert str(rt.MAX_AGENT_HOPS) in notice
    for word in ("异常", "故障", "攻击", "循环"):
        assert word not in notice


def test_client_cannot_raise_the_server_limit():
    """客户端可以展示这个数，不能提高它。"""
    assert rt.hops_exhausted(rt.MAX_AGENT_HOPS, limit=99) is False  # 服务端自己放宽是可以的
    # 但服务端的默认判定不看任何客户端输入：函数签名里根本没有这个入口。
    assert rt.hops_exhausted(rt.MAX_AGENT_HOPS) is True


def test_queue_has_a_visible_ceiling_instead_of_silent_drops():
    rt.can_enqueue(rt.MAX_QUEUE_PER_PROJECT - 1)
    with pytest.raises(ProjectRejected):
        rt.can_enqueue(rt.MAX_QUEUE_PER_PROJECT)


def test_backoff_is_exponential_and_capped():
    assert rt.retry_delay(1) == rt.BACKOFF_BASE_SECONDS
    assert rt.retry_delay(2) > rt.retry_delay(1)
    assert rt.retry_delay(50) == rt.BACKOFF_MAX_SECONDS
    assert rt.should_retry(rt.MAX_TURN_ATTEMPTS) is False


def test_batching_keeps_every_event_auditable():
    """合成上下文可以，合掉审计关联不行。"""
    events = [
        rt.TriggerEvent(project_id=1, seq=s, reason=rt.TRIGGER_MENTION,
                        agent_member_id=11, requester_uid=MATE, message_id=100 + s)
        for s in (3, 1, 2)
    ]
    merged = rt.merge_batch(events)

    assert [e.seq for e in merged] == [1, 2, 3]
    assert all(e.reason and e.requester_uid and e.message_id for e in merged)


def test_batching_dedupes_and_caps():
    dup = rt.TriggerEvent(project_id=1, seq=5, reason=rt.TRIGGER_MENTION,
                          agent_member_id=11, requester_uid=MATE)
    merged = rt.merge_batch([dup, dup])
    assert len(merged) == 1

    many = [
        rt.TriggerEvent(project_id=1, seq=s, reason=rt.TRIGGER_MENTION,
                        agent_member_id=11, requester_uid=MATE)
        for s in range(rt.MAX_BATCH_EVENTS + 10)
    ]
    capped = rt.merge_batch(many)
    assert len(capped) == rt.MAX_BATCH_EVENTS
    # 先来先答：截断保留最早的一批，剩下的还在队列里。
    assert capped[0].seq == 0


# ── turn 状态机与迟到心跳 ──────────────────────────────────────────────────

def test_turn_state_machine_allows_only_forward_moves():
    assert rt.can_transition(rt.TURN_QUEUED, rt.TURN_RUNNING) is True
    assert rt.can_transition(rt.TURN_RUNNING, rt.TURN_COMPLETED) is True
    assert rt.can_transition(rt.TURN_COMPLETED, rt.TURN_RUNNING) is False
    assert rt.can_transition(rt.TURN_QUEUED, "whatever") is False
    with pytest.raises(ProjectRejected):
        rt.require_transition(rt.TURN_CANCELLED, rt.TURN_RUNNING)


def test_completed_tombstone_beats_a_late_heartbeat():
    """迟到的心跳不能把已结束的 turn 重新显示成运行中。

    墓碑优先于任何时间戳：客户端的钟可能是错的，而「已完成」这件事是确定的。
    """
    done = {"status": rt.TURN_COMPLETED, "status_seq": 5,
            "updated_at": datetime(2026, 9, 4, 10, 0, 0)}
    late = {"status": rt.TURN_RUNNING, "status_seq": 99,
            "updated_at": datetime(2026, 9, 4, 23, 0, 0)}

    assert rt.accept_status_update(done, late) is False


def test_status_watermark_only_moves_forward():
    running = {"status": rt.TURN_RUNNING, "status_seq": 4,
               "updated_at": datetime(2026, 9, 4, 10, 0, 0)}
    older = {"status": rt.TURN_COMPLETED, "status_seq": 3,
             "updated_at": datetime(2026, 9, 4, 9, 0, 0)}
    newer = {"status": rt.TURN_COMPLETED, "status_seq": 5,
             "updated_at": datetime(2026, 9, 4, 11, 0, 0)}

    assert rt.accept_status_update(running, older) is False
    assert rt.accept_status_update(running, newer) is True


def test_same_seq_repeat_is_not_information():
    same = {"status": rt.TURN_RUNNING, "status_seq": 4, "updated_at": None}
    assert rt.accept_status_update(same, dict(same, status=rt.TURN_COMPLETED)) is False


# ── P0-E 写回身份 ─────────────────────────────────────────────────────────

def _turn(**over):
    row = {
        "id": 7,
        "project_id": 1,
        "member_id": 11,
        "owner_uid": OWNER,
        "agent_label": "小助手",
        "status": rt.TURN_RUNNING,
        "trigger_request_id": "req-abc",
        "lease_expires_at": datetime.now() + timedelta(minutes=5),
    }
    row.update(over)
    return row


def test_writeback_identity_comes_from_the_lease_only():
    """能自称 agent 的接口就是决策 #45 说不能做的那种设计。"""
    ident = rt.writeback_identity(_turn())

    assert ident.sender_kind == "agent"
    # 归属挂主人：群聊消耗算主人的，记账口径才接得上。
    assert ident.sender_uid == OWNER
    assert ident.sender_agent_label == "小助手"
    assert ident.trigger_request_id == "req-abc"


def test_lease_rejects_cross_project_writeback():
    with pytest.raises(ProjectRejected):
        rt.require_lease(_turn(project_id=1), project_id=2, uid=OWNER)


def test_lease_rejects_impersonating_another_owner():
    with pytest.raises(ProjectRejected):
        rt.require_lease(_turn(owner_uid=OWNER), project_id=1, uid=MATE)


def test_finished_turn_cannot_write_back():
    for status in (rt.TURN_QUEUED, rt.TURN_COMPLETED, rt.TURN_CANCELLED, rt.TURN_FAILED):
        with pytest.raises(ProjectRejected):
            rt.require_lease(_turn(status=status), project_id=1, uid=OWNER)


def test_missing_lease_is_a_rejection_not_a_default_identity():
    with pytest.raises(ProjectRejected):
        rt.require_lease(None, project_id=1, uid=OWNER)


# ── 决策 #45：群聊 turn 的工具集 ──────────────────────────────────────────

@pytest.mark.parametrize("forbidden", sorted(rt.GROUP_TURN_FORBIDDEN_TOOL_KINDS))
def test_group_turn_never_carries_capability_tools(forbidden):
    """群聊 agent 的 tool schema 里不得出现 MCP、skill 与个人凭据。

    能力不跨界，只有产物跨界：要真执行就回主人私聊，走 P0-6 的原文确认。
    """
    with pytest.raises(ProjectRejected):
        rt.assert_group_toolset(["chat", forbidden])


def test_group_turn_allows_discussion_and_project_tools():
    rt.assert_group_toolset(["chat", "project"])


def test_toolset_check_is_a_whitelist():
    """黑名单会在每次新增工具类别时静默漏一个，而漏掉的那次没有任何报错。"""
    with pytest.raises(ProjectRejected):
        rt.assert_group_toolset(["chat", "some_new_tool_kind_nobody_thought_about"])
