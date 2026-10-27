"""项目群聊的恢复协议（阶段 27-B）

守的是四条**不会报错**的东西：水位只前进、去重集合有界、清场之后旧响应不算数、
失权与抖动的处理相反。

这四条错了都不会抛异常，只会表现成「群里少一条消息」「同一条出现两次」
「切了账号还看得到上一个人的内容」「一个停不下来的后台轮询」——全都要等人发现。
"""

from __future__ import annotations

from tools.project_sync import (MAX_DEDUPE_KEYS, POLL_ERROR_MAX_SECONDS,
                                POLL_IDLE_SECONDS, POLL_MIN_SECONDS,
                                ProjectCursor, ProjectSync, message_key,
                                next_delay)


def _msg(seq, project_id=1, **over):
    row = {"id": 1000 + seq, "project_id": project_id, "seq": seq, "content": f"第 {seq} 条"}
    row.update(over)
    return row


# ── 水位与去重 ────────────────────────────────────────────────────────────

def test_baseline_new_messages_pass_through():
    """基线。没有这条的话，下面的「被去重了」可能只是因为它把什么都丢掉。"""
    cursor = ProjectCursor("demo")

    fresh = cursor.admit([_msg(1), _msg(2)])

    assert [m["seq"] for m in fresh] == [1, 2]
    assert cursor.seq == 2


def test_overlap_between_catchup_and_live_is_deduped():
    """补拉窗口与实时流允许重叠，重叠部分靠 id 去重收尾。"""
    cursor = ProjectCursor("demo")
    cursor.admit([_msg(1), _msg(2), _msg(3)])

    again = cursor.admit([_msg(2), _msg(3), _msg(4)])

    assert [m["seq"] for m in again] == [4]
    assert cursor.seq == 4


def test_watermark_never_goes_backwards():
    """旧响应晚到不得把水位拉回去 —— 否则下一轮会把已显示的消息再拉一遍。"""
    cursor = ProjectCursor("demo")
    cursor.admit([_msg(10)])

    cursor.admit([_msg(4)])

    assert cursor.seq == 10


def test_dedupe_set_is_bounded():
    """不设上限的话它随会话时长单调增长，而这类泄漏在测试里永远看不见。"""
    cursor = ProjectCursor("demo")

    cursor.admit([_msg(s) for s in range(MAX_DEDUPE_KEYS * 2)])

    assert cursor.seen_count <= MAX_DEDUPE_KEYS


def test_messages_without_seq_are_dropped():
    """没有 seq 就没有水位，留着它只会让去重靠内容猜。"""
    cursor = ProjectCursor("demo")

    fresh = cursor.admit([{"id": 1, "content": "没有 seq"}, _msg(1)])

    assert [m["seq"] for m in fresh] == [1]


def test_dedupe_key_is_scoped_to_the_project():
    """项目 A 的游标不该因为项目 B 的写入而跳号。"""
    assert message_key(1, 7) != message_key(2, 7)


# ── 账号切换与清场 ────────────────────────────────────────────────────────

def test_identity_change_clears_every_watermark():
    """留着上一个账号的水位，新账号会从一个凭空的 seq 开始拉。"""
    sync = ProjectSync()
    sync.note_identity("gw#aaa")
    sync.admit("demo", [_msg(9)])

    changed = sync.note_identity("gw#bbb")

    assert changed is True
    assert sync.cursor("demo").seq == 0


def test_same_identity_is_not_a_reset():
    sync = ProjectSync()
    sync.note_identity("gw#aaa")
    sync.admit("demo", [_msg(9)])

    assert sync.note_identity("gw#aaa") is False
    assert sync.cursor("demo").seq == 9


def test_signing_out_counts_as_an_identity_change():
    """没登录时指纹是空串，与任何真实身份都不相等。"""
    sync = ProjectSync()
    sync.note_identity("gw#aaa")

    assert sync.note_identity("") is True


def test_late_response_from_a_previous_epoch_is_discarded():
    """旧项目/旧账号的请求晚到，不能覆盖用户刚切过去的新状态。"""
    sync = ProjectSync()
    sync.note_identity("gw#aaa")
    epoch = sync.epoch

    sync.reset(reason="切号")  # 请求还飞在路上
    fresh, _ = sync.admit("demo", [_msg(1)], epoch=epoch)

    assert fresh == []
    assert sync.cursor("demo").seq == 0


def test_response_from_the_current_epoch_is_accepted():
    sync = ProjectSync()
    sync.note_identity("gw#aaa")

    fresh, next_seq = sync.admit("demo", [_msg(1)], epoch=sync.epoch)

    assert [m["seq"] for m in fresh] == [1]
    assert next_seq == 1


# ── 失权是终止条件，不是错误 ──────────────────────────────────────────────

def test_forgetting_a_project_stops_it_from_being_tracked():
    """归档 / 被移除 / 停用之后不该再拉 —— 那是终止条件，重试没有意义。"""
    sync = ProjectSync()
    sync.note_identity("gw#aaa")
    sync.admit("demo", [_msg(1)])

    sync.forget("demo", reason="revoked")

    assert sync.is_revoked("demo") is True
    assert sync.revoked_reason("demo") == "revoked"
    fresh, _ = sync.admit("demo", [_msg(2)])
    assert fresh == []


def test_reset_clears_revocations_too():
    """换个账号之后，上一个账号的失权结论不该继续生效。"""
    sync = ProjectSync()
    sync.note_identity("gw#aaa")
    sync.forget("demo", reason="revoked")

    sync.note_identity("gw#bbb")

    assert sync.is_revoked("demo") is False


# ── 退避 ──────────────────────────────────────────────────────────────────

def test_idle_backoff_is_capped():
    assert next_delay() == POLL_MIN_SECONDS
    assert next_delay(idle_rounds=1) > POLL_MIN_SECONDS
    assert next_delay(idle_rounds=999) == POLL_IDLE_SECONDS


def test_errors_back_off_harder_than_idling():
    """连不上网关时慢退避，而不是继续按空闲节奏敲。"""
    assert next_delay(error_rounds=1) >= next_delay(idle_rounds=1)
    assert next_delay(error_rounds=99) == POLL_ERROR_MAX_SECONDS


def test_error_backoff_wins_over_idle():
    assert next_delay(idle_rounds=1, error_rounds=3) == next_delay(error_rounds=3)
