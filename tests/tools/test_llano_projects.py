"""协作项目的 sidecar 侧客户端（阶段 16）

桌面端的数据通路是 渲染进程 --WS--> sidecar --HTTPS+APIKey--> 审计网关。
渲染进程拿不到 API Key，所以这一层代取。

守两件事：**读写的失败处理相反**，以及**认领与确认不能被合并**。
"""

from __future__ import annotations

import json

import pytest

from tools import llano_projects as lp
from tools.llano_projects import ProjectsUnavailable


@pytest.fixture
def _net(monkeypatch):
    calls = []
    responses = {}

    def fake_request(path, *, method="GET", body=None):
        calls.append((method, path, json.loads(body) if body else None))
        if path in responses:
            value = responses[path]
            if isinstance(value, Exception):
                raise value
            return value
        raise ProjectsUnavailable(f"没有为 {path} 编排响应")

    monkeypatch.setattr(lp, "_request", fake_request)
    return calls, responses


# ── 读：失败返回空，不让界面崩 ────────────────────────────────────────────

def test_reads_preserve_failure_for_the_sidecar_cache(_net):
    for call in [lp.list_projects, lambda: lp.list_members("p1"), lambda: lp.list_tasks("p1")]:
        with pytest.raises(ProjectsUnavailable):
            call()
    # History convenience API retains its existing fail-soft contract.
    assert lp.list_messages("p1") == []


def test_a_malformed_response_also_degrades(_net):
    """网关返回体变形（版本不匹配、被中间层改过）时同样不能崩。"""
    calls, responses = _net
    for bad in [{}, {"projects": None}, {"projects": "not a list"}, []]:
        responses["/v1/projects"] = bad
        assert lp.list_projects() == []


def test_reads_pass_through_when_they_work(_net):
    """反向基线：上面的「返回空」不能是因为这个函数永远返回空。"""
    calls, responses = _net
    responses["/v1/projects"] = {"projects": [{"code": "q4", "name": "Q4"}]}

    assert lp.list_projects()[0]["code"] == "q4"


def test_message_cursor_uses_the_project_local_seq(_net):
    """用全局 id 当游标会因为**别的项目**的写入而跳号，界面上看起来像丢了消息。"""
    calls, responses = _net
    responses["/v1/projects/q4/messages?after_seq=7&limit=50"] = {"messages": []}

    lp.list_messages("q4", after_seq=7)

    assert "after_seq=7" in calls[0][1]


def test_codes_are_url_escaped(_net):
    """项目标识已经在服务端被限死在 [a-z0-9-]，但这一层不能依赖那个假设：
    没转义的字符串拼进 URL，`../` 会变成打到别的端点上的请求。"""
    calls, responses = _net
    responses["/v1/projects/..%2F..%2Fadmin/messages?after_seq=0&limit=50"] = {"messages": []}

    lp.list_messages("../../admin")

    assert calls[0][1].startswith("/v1/projects/..%2F..%2Fadmin/")


# ── 写：失败抛出去，不能静默 ──────────────────────────────────────────────

def test_writes_raise_instead_of_swallowing(_net):
    """用户按了发送却什么都没发生，必须说清楚为什么。

    与读路径相反：读失败静默让路是对的（一次抖动不该让界面崩），
    写失败静默是灾难 —— 他以为消息发出去了。
    """
    for call in [
        lambda: lp.say("q4", "hi"),
        lambda: lp.create_task("q4", "t"),
        lambda: lp.claim("q4", 1),
        lambda: lp.confirm("q4", 1),
        lambda: lp.create_project("q4"),
        lambda: lp.retry_repository("q4"),
    ]:
        with pytest.raises(ProjectsUnavailable):
            call()


def test_repository_retry_uses_the_scoped_endpoint(_net):
    calls, responses = _net
    responses["/v1/projects/q4/repository/retry"] = {"status": "pending"}

    assert lp.retry_repository("q4")["status"] == "pending"
    assert calls == [("POST", "/v1/projects/q4/repository/retry", {})]


def test_the_client_never_sends_the_untrusted_flag(_net):
    """服务端按来源自己定。

    客户端能提交这个标记的话，注入一句「把 untrusted 设成 false」就绕过整条
    P0-6 防线 —— 与决策 #45「身份绝不能由 agent 提供」是同一类问题。
    """
    calls, responses = _net
    responses["/v1/projects/q4/tasks"] = {"id": 1, "untrusted": True}

    lp.create_task("q4", "导出销量", "正文")

    assert "untrusted" not in calls[0][2]


def test_a_chat_sourced_task_says_so(_net):
    calls, responses = _net
    responses["/v1/projects/q4/tasks"] = {"id": 1}

    lp.create_task("q4", "t", from_chat=True)
    assert calls[0][2]["source_kind"] == "chat"

    calls.clear()
    lp.create_task("q4", "t", from_chat=False)
    assert calls[0][2]["source_kind"] == "manual"


def test_the_client_does_no_redaction_of_its_own(_net):
    """脱敏发生在**服务端**。

    指望发送方的客户端替换等于把一条防线交给被防的那一侧 —— 它跑在员工自己的
    机器上，而接收方是别的部门、甚至别的公司的人（P0-2）。所以原文原样送出去，
    由网关替换。
    """
    calls, responses = _net
    responses["/v1/projects/q4/messages"] = {"id": 1, "redacted": True}

    lp.say("q4", "LLANO Agent的报表")

    assert calls[0][2]["content"] == "LLANO Agent的报表", "客户端不该自己动内容"


# ── P0-6：认领与确认不能被合并 ────────────────────────────────────────────

def test_claim_and_confirm_are_two_separate_calls(_net):
    """认领只到 claimed，确认是另一次调用。

    合并成一步就把「人在环」那一下抹掉了 —— 而群里发一个夹带注入的任务描述、
    诱导 agent 认领、回私聊执行，正是那一下要挡的东西。
    """
    calls, responses = _net
    responses["/v1/projects/q4/tasks/1/claim"] = {
        "status": "claimed",
        "confirmation": {"body": "原文", "untrusted": True, "reminder": "…"},
    }

    result = lp.claim("q4", 1)

    assert result["status"] == "claimed"
    assert len(calls) == 1, "认领不该顺手把确认也调了"
    assert result["confirmation"]["body"] == "原文"


def test_no_convenience_wrapper_collapses_the_two_steps():
    """刻意不提供 claim_and_confirm 之类的便利函数。

    便利函数会被用，然后防线就没了。这条扫的是模块的公开面 —— 哪天有人「顺手」
    加一个，这里报红。
    """
    exported = {n for n in dir(lp) if not n.startswith("_")}
    for banned in ["claim_and_confirm", "claim_and_start", "take_task", "auto_claim"]:
        assert banned not in exported, f"不该有 {banned}：它会把人在环那一下抹掉"


def test_confirm_hands_the_untrusted_flag_back(_net):
    """它要跟着任务描述一起进入私聊上下文，不能因为「这是主人自己的会话」就清掉。"""
    calls, responses = _net
    responses["/v1/projects/q4/tasks/1/confirm"] = {"status": "active", "untrusted": True}

    assert lp.confirm("q4", 1)["untrusted"] is True


def test_it_reuses_the_skill_hub_transport(_net):
    """同一把 Key、同一个 origin、同一套错误话术。

    另起一份 HTTP 封装会带来两套超时、两套错误处理，而它们会各自漂移 ——
    漂移的表现是「技能库连得上、群聊连不上」，而两者其实是同一个网关。
    """
    import inspect

    source = inspect.getsource(lp)

    assert "from tools.llano_skill_hub import" in source
    assert "urlopen" not in source, "不该自己再开一条 HTTP 通路"


# ── 群聊令牌的本地缓存（2026-09-07）────────────────────────────────────────
#
# 网关那个接口每次都无条件新签一把，8 小时有效且不回收 —— 而 `run_turn` 每轮都要
# 一把。阶段 27 验收里跑 10 轮就在 api_keys 里留下 10 行。少签就少留。


@pytest.fixture
def _identity(monkeypatch):
    """把身份指纹固定住，并给出一个能改它的把手。"""
    box = {"value": "gw#aaaa"}
    monkeypatch.setattr(lp, "identity_fingerprint", lambda: box["value"])
    lp.clear_group_sessions()
    yield box
    lp.clear_group_sessions()


def _session(expires_at="2099-01-01T00:00:00"):
    return {"api_key": "sk-llano-group", "scope": "group", "expires_at": expires_at}


def test_group_session_is_reused_instead_of_signing_one_per_turn(_net, _identity):
    calls, responses = _net
    responses["/v1/projects/q4/session"] = _session()

    first = lp.group_session("q4")
    second = lp.group_session("q4")

    assert first == second
    signed = [c for c in calls if c[1].endswith("/session")]
    assert len(signed) == 1, f"应该只签一次，实际签了 {len(signed)} 次"


def test_each_project_gets_its_own_token(_net, _identity):
    """令牌的作用域绑在项目上，拿 A 项目的去 B 项目用会被网关拒。"""
    calls, responses = _net
    responses["/v1/projects/q4/session"] = _session()
    responses["/v1/projects/q3/session"] = _session()

    lp.group_session("q4")
    lp.group_session("q3")

    assert len({c[1] for c in calls if c[1].endswith("/session")}) == 2


def test_switching_account_does_not_hand_over_the_previous_token(_net, _identity):
    """退出/换账号之后不能复用上一个身份的令牌 —— 那是别人的凭据。"""
    calls, responses = _net
    responses["/v1/projects/q4/session"] = _session()

    lp.group_session("q4")
    _identity["value"] = "gw#bbbb"
    lp.group_session("q4")

    assert len([c for c in calls if c[1].endswith("/session")]) == 2


def test_a_token_close_to_expiry_is_not_reused(_net, _identity):
    """临近过期就重签。喂一把马上要死的令牌给模型调用，失败发生在最贵的那一步。"""
    calls, responses = _net
    responses["/v1/projects/q4/session"] = _session(expires_at="2000-01-01T00:00:00")

    lp.group_session("q4")
    lp.group_session("q4")

    assert len([c for c in calls if c[1].endswith("/session")]) == 2


def test_a_response_without_a_key_is_not_cached(_net, _identity):
    """缓存一个空壳只会让接下来一小时里每一轮都拿到同一个空壳。"""
    calls, responses = _net
    responses["/v1/projects/q4/session"] = {"scope": "group"}

    lp.group_session("q4")
    lp.group_session("q4")

    assert len([c for c in calls if c[1].endswith("/session")]) == 2


def test_clear_group_sessions_can_drop_just_one_project(_net, _identity):
    calls, responses = _net
    responses["/v1/projects/q4/session"] = _session()
    responses["/v1/projects/q3/session"] = _session()

    lp.group_session("q4")
    lp.group_session("q3")
    lp.clear_group_sessions(code="q4")
    lp.group_session("q4")
    lp.group_session("q3")

    signed = [c[1] for c in calls if c[1].endswith("/session")]
    assert signed.count("/v1/projects/q4/session") == 2
    assert signed.count("/v1/projects/q3/session") == 1


def test_the_cache_window_stays_far_from_the_server_ttl():
    """服务端给 8 小时，这里只缓存 1 小时。

    贴着过期边界缓存的话，一次客户端与服务端的时钟偏差就足以让我们把一把已经死掉
    的令牌喂给模型调用。留 7 小时余量是有意的，不是随手写的数。
    """
    assert lp._GROUP_SESSION_MAX_CACHE_SECONDS <= 3600
    assert lp._GROUP_SESSION_EXPIRY_MARGIN_SECONDS >= 300


def test_pull_my_agent_never_sends_a_uid(_net):
    """能拉别人的 agent 等于替别人的凭据做决定。服务端一律用调用者自己的 uid，
    客户端连字段都不发 —— 让这条规则在代码里也读得出来。"""
    calls, responses = _net
    responses["/v1/projects/q4/members"] = {"id": 7, "rejoined": False}

    lp.pull_my_agent("q4", "我的助手")

    _, path, body = calls[-1]
    assert path == "/v1/projects/q4/members"
    assert body == {"member_kind": "agent", "agent_label": "我的助手"}
    assert "user_uid" not in body and "username" not in body
