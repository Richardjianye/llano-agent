"""触发水位在服务端（2026-09-07）

## 这一条是真机上炸出来的

桌面端的 tick 每 6 秒拿 `after_seq=0` 问一次「有没有 @ 我的消息」，而 seq=2 那条
@ **永远**在 0 之后 —— 同一条 @ 被反复消费，agent 每 6 秒回一句，一路刷到 8 跳
熔断才停，中间还出现两条一字不差的回复（两轮拿到了同一个触发）。

熔断确实拦住了（尾部连续 agent 消息正好 8 条），但熔断是最后一道 —— 它不该被
一次正常的 @ 打到。

## 为什么水位不能交给客户端

不是因为客户端会写错，是因为**客户端记不住**：它会重启（内存里的水位没了），
同一个人可能开着两个客户端（两份水位各自推进），而它还是被防的那一侧。服务端
自己有答案：`project_agent_turns.trigger_seq` 就是「这一轮是被哪条消息叫起来的」。
"""

from __future__ import annotations

import inspect

from db.selects import project_turn as selects
from router.v1 import project_turns as router_mod


def test_the_lease_floors_the_client_watermark():
    """客户端只能往后收窄，不能往前放宽 —— 所以是 `max`，不是直接采纳。"""
    source = inspect.getsource(router_mod.lease_turn)

    assert "last_handled_trigger_seq" in source
    assert "max(int(after_seq or 0)" in source, "客户端传的值不能直接用"


def test_the_floor_is_applied_before_the_mention_scan():
    """先抬水位再扫消息。顺序反了等于没抬。"""
    source = inspect.getsource(router_mod.lease_turn)

    floor_at = source.index("last_handled_trigger_seq")
    scan_at = source.index("pending_mention_triggers(")

    assert floor_at < scan_at


def test_the_watermark_counts_failed_and_cancelled_turns_too():
    """失败的 turn 也算消费过这条触发。

    放过它去重试，恢复的就是刚才那个刷屏 —— 模型持续失败时，「自动重试」与
    「每 6 秒撞一次墙」是同一件事。人再 @ 一次就能重来，而那一下正好是
    「确实还需要」的证据。
    """
    body = inspect.getsource(selects.last_handled_trigger_seq).split(chr(34) * 3)[-1]

    assert "status" not in body, "按状态过滤就会让失败的 turn 反复重试"
    assert "func.max" in body


def test_the_watermark_is_per_agent_not_per_project():
    """一个项目里可以有多个 agent。按项目记的话，A 被 @ 过就会让 B 永远收不到。"""
    body = inspect.getsource(selects.last_handled_trigger_seq).split(chr(34) * 3)[-1]

    assert "ProjectAgentTurn.project_id == project_id" in body
    assert "ProjectAgentTurn.member_id == member_id" in body
