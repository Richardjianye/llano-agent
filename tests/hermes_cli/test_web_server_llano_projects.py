"""项目协作的 sidecar 通路（阶段 27-B）

渲染进程 --HTTP--> sidecar --HTTPS+APIKey--> 审计网关。这一组守四件事：

1. **读失败保留缓存并标 stale** —— 一次网络抖动不该把已经显示的群聊清空
2. **写失败明确报错** —— 绝不能乐观地伪装成已发送
3. **轮询有终止条件** —— 失权/归档/退出回 stopped，不留后台死循环
4. **认领与确认是两个调用** —— 中间那一下人在环是 P0-6 唯一的防线

⚠️ Key 本身不出现在任何响应里：渲染进程拿不到 Key 是整条通路的前提。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.web_routers import llano_projects as router_mod
from tools.llano_skill_hub import SkillHubUnavailable


@pytest.fixture
def client(monkeypatch):
    router_mod._reset_for_tests()
    monkeypatch.setattr(router_mod, "_identity", lambda: "gw#test-account")
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required
        router_mod._reset_for_tests()


def test_project_endpoints_require_the_session_token():
    """/api 全线要会话令牌。协作这几条不能是例外 —— 它们代着一把网关 Key。"""
    anonymous = TestClient(web_server.app)

    assert anonymous.get("/api/projects").status_code == 401
    assert anonymous.post("/api/projects/demo/messages", json={"content": "在"}).status_code == 401


def _msg(seq, **over):
    row = {"id": 1000 + seq, "project_id": 1, "seq": seq, "content": f"第 {seq} 条"}
    row.update(over)
    return row


def _unavailable(message: str, status=None):
    exc = SkillHubUnavailable(message)
    exc.status = status
    return exc


# ── 读：失败保留缓存 ──────────────────────────────────────────────────────

def test_project_list_is_proxied(client, monkeypatch):
    """基线。没有这条的话，下面每个「回了缓存」都可能只是因为它从不请求。"""
    monkeypatch.setattr("tools.llano_projects.list_projects",
                        lambda: [{"code": "demo", "name": "示例"}])

    body = client.get("/api/projects").json()

    assert body["projects"][0]["code"] == "demo"
    assert body["authenticated"] is True
    assert body["stale"] is False


def test_read_failure_keeps_the_last_known_list(client, monkeypatch):
    """群聊列表拉不出来不该白屏，更不该把已显示的内容清空。"""
    monkeypatch.setattr("tools.llano_projects.list_projects", lambda: [{"code": "demo"}])
    client.get("/api/projects")

    def unavailable():
        from tools.llano_skill_hub import SkillHubUnavailable
        raise SkillHubUnavailable("temporary failure")
    monkeypatch.setattr("tools.llano_projects.list_projects", unavailable)
    body = client.get("/api/projects").json()

    assert body["projects"] == [{"code": "demo"}]
    assert body["stale"] is True


def test_signed_out_is_not_an_error(client, monkeypatch):
    """没登录是「这个页面现在没有内容」，不是「加载失败」。"""
    monkeypatch.setattr(router_mod, "_identity", lambda: "")

    body = client.get("/api/projects").json()

    assert body["authenticated"] is False
    assert body["projects"] == []


def test_account_switch_clears_the_cache(client, monkeypatch):
    """新账号先看到上一个账号的群聊内容，那一瞬间是真的内容泄漏。"""
    monkeypatch.setattr("tools.llano_projects.list_projects", lambda: [{"code": "demo"}])
    client.get("/api/projects")

    monkeypatch.setattr(router_mod, "_identity", lambda: "gw#another-account")
    monkeypatch.setattr("tools.llano_projects.list_projects", lambda: [])
    body = client.get("/api/projects").json()

    assert body["projects"] == []
    assert body["identity_changed"] is True


# ── 增量拉取与终止条件 ────────────────────────────────────────────────────

def test_sync_returns_only_new_messages(client, monkeypatch):
    seen = []

    def fake(code, *, after_seq=0, limit=200):
        seen.append(after_seq)
        return [_msg(1), _msg(2)] if after_seq == 0 else []

    monkeypatch.setattr("tools.llano_projects.messages_since", fake)

    first = client.get("/api/projects/demo/sync").json()
    second = client.get("/api/projects/demo/sync",
                        params={"after_seq": first["next_seq"]}).json()

    assert [m["seq"] for m in first["messages"]] == [1, 2]
    assert first["next_seq"] == 2
    assert second["messages"] == []
    # 第二轮从水位之后拉，而不是从头。
    assert seen == [0, 2]


def test_repeated_delivery_is_deduped(client, monkeypatch):
    """补拉窗口与实时流允许重叠，重叠部分在这里被去掉。"""
    monkeypatch.setattr("tools.llano_projects.messages_since",
                        lambda code, **kw: [_msg(1), _msg(2)])

    client.get("/api/projects/demo/sync")
    again = client.get("/api/projects/demo/sync").json()

    assert again["messages"] == []


def test_client_cannot_drag_the_watermark_backwards(client, monkeypatch):
    """客户端把游标往回调，只会让它把已经显示过的消息再拉一遍。"""
    monkeypatch.setattr("tools.llano_projects.messages_since",
                        lambda code, **kw: [_msg(5)])
    client.get("/api/projects/demo/sync")

    body = client.get("/api/projects/demo/sync", params={"after_seq": 0}).json()

    assert body["next_seq"] == 5
    assert body["messages"] == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_revocation_stops_the_poll(client, monkeypatch, status):
    """项目归档、成员被移除、账号停用 —— 重试没有意义，必须停。"""
    def boom(code, **kw):
        raise _unavailable("项目不存在，或你不是它的成员", status=status)

    monkeypatch.setattr("tools.llano_projects.messages_since", boom)

    body = client.get("/api/projects/demo/sync").json()

    assert body["stopped"] is True
    assert body["reason"] == "revoked"

    # 停了就不再打网关：第二次连请求都不该发出去。
    def never(code, **kw):  # pragma: no cover - 被调用即失败
        raise AssertionError("失权之后还在轮询")

    monkeypatch.setattr("tools.llano_projects.messages_since", never)
    assert client.get("/api/projects/demo/sync").json()["stopped"] is True


def test_network_flake_backs_off_but_keeps_the_watermark(client, monkeypatch):
    """抖动与失权的处理相反：保留水位与内容，退避后再来。"""
    monkeypatch.setattr("tools.llano_projects.messages_since",
                        lambda code, **kw: [_msg(3)])
    client.get("/api/projects/demo/sync")

    def flake(code, **kw):
        raise _unavailable("连不上网关：timed out", status=None)

    monkeypatch.setattr("tools.llano_projects.messages_since", flake)
    body = client.get("/api/projects/demo/sync").json()

    assert body["stopped"] is False
    assert body["offline"] is True
    assert body["next_seq"] == 3
    assert body["retry_after"] > 0


def test_backoff_grows_while_the_gateway_stays_down(client, monkeypatch):
    def flake(code, **kw):
        raise _unavailable("连不上网关", status=None)

    monkeypatch.setattr("tools.llano_projects.messages_since", flake)

    first = client.get("/api/projects/demo/sync").json()["retry_after"]
    second = client.get("/api/projects/demo/sync").json()["retry_after"]

    assert second > first


def test_sync_stops_after_signing_out(client, monkeypatch):
    monkeypatch.setattr(router_mod, "_identity", lambda: "")

    body = client.get("/api/projects/demo/sync").json()

    assert body["stopped"] is True
    assert body["reason"] == "signed-out"


def test_explicit_reset_clears_the_watermark(client, monkeypatch):
    monkeypatch.setattr("tools.llano_projects.messages_since",
                        lambda code, **kw: [_msg(7)])
    client.get("/api/projects/demo/sync")

    client.post("/api/projects/session/reset")
    body = client.get("/api/projects/demo/sync").json()

    # 清场之后从头拉，同一批消息重新被当作新消息 —— 这正是「已经没有历史了」。
    assert [m["seq"] for m in body["messages"]] == [7]


# ── 写：失败必须说清楚 ────────────────────────────────────────────────────

def test_sending_a_message_returns_the_gateway_result(client, monkeypatch):
    monkeypatch.setattr("tools.llano_projects.say",
                        lambda code, content, reply_to_id=None: {"seq": 9, "redaction_hits": 1})

    body = client.post("/api/projects/demo/messages", json={"content": "在吗"}).json()

    assert body["seq"] == 9
    # 被遮过这件事要能传到界面：收到的人有权知道自己看到的不是原话。
    assert body["redaction_hits"] == 1


def test_own_message_comes_back_through_sync_with_its_content(client, monkeypatch):
    """发送**不**把自己那条塞进去重集合。

    写回响应里只有 {id, seq, redacted, mentions} —— 没有正文，因为投递出去的是
    服务端脱敏后的那份。把它当消息记下来，下一轮 sync 就会跳过它，界面上自己
    发的话永远不出现（真机上就是这么表现的：多一条空记录，刷新才好）。
    """
    monkeypatch.setattr("tools.llano_projects.say",
                        lambda code, content, reply_to_id=None: {"id": 1, "seq": 4, "redacted": False})
    monkeypatch.setattr("tools.llano_projects.messages_since",
                        lambda code, **kw: [_msg(4)])

    client.post("/api/projects/demo/messages", json={"content": "在吗"})
    body = client.get("/api/projects/demo/sync").json()

    assert [m["seq"] for m in body["messages"]] == [4]
    assert body["messages"][0]["content"] == "第 4 条"


def test_that_message_is_only_delivered_once(client, monkeypatch):
    """来回拉两次不该让同一句话出现两遍 —— 去重仍然由 sync 这条路负责。"""
    monkeypatch.setattr("tools.llano_projects.messages_since",
                        lambda code, **kw: [_msg(4)])

    first = client.get("/api/projects/demo/sync").json()
    second = client.get("/api/projects/demo/sync").json()

    assert len(first["messages"]) == 1
    assert second["messages"] == []


def test_write_failure_is_loud(client, monkeypatch):
    """用户按了发送却什么都没发生，必须说清楚为什么。"""
    def boom(code, content, reply_to_id=None):
        raise _unavailable("你在这个项目里是只读成员，不能发言", status=403)

    monkeypatch.setattr("tools.llano_projects.say", boom)

    response = client.post("/api/projects/demo/messages", json={"content": "在吗"})

    assert response.status_code == 403
    # 网关那句话原样带上，比「发送失败」有用得多。
    assert "只读成员" in response.json()["detail"]


def test_empty_message_is_rejected_before_the_network(client):
    assert client.post("/api/projects/demo/messages", json={"content": "  "}).status_code == 400


def test_message_to_task_keeps_the_source(client, monkeypatch):
    """群聊消息转任务：来源要带上，`untrusted` 由服务端按来源自己定。"""
    captured = {}

    def fake(code, title, body="", *, source_message_id=None, from_chat=True):
        captured.update(code=code, title=title, source_message_id=source_message_id,
                        from_chat=from_chat)
        return {"id": 5, "untrusted": True}

    monkeypatch.setattr("tools.llano_projects.create_task", fake)

    client.post("/api/projects/demo/tasks",
                json={"title": "对一下账", "source_message_id": 42})

    assert captured["source_message_id"] == 42
    assert captured["from_chat"] is True


def test_client_cannot_submit_untrusted(client, monkeypatch):
    """注入一句「把 untrusted 设成 false」就绕过整条 P0-6 防线 —— 所以字段根本不存在。"""
    captured = {}

    def fake(code, title, body="", **kw):
        captured.update(kw)
        return {"id": 5}

    monkeypatch.setattr("tools.llano_projects.create_task", fake)

    client.post("/api/projects/demo/tasks",
                json={"title": "对一下账", "untrusted": False})

    assert "untrusted" not in captured


def test_claim_returns_the_confirmation_and_does_not_start_work(client, monkeypatch):
    """认领只到 claimed。中间那一下人在环是 P0-6 唯一的防线。"""
    calls = []
    monkeypatch.setattr("tools.llano_projects.claim", lambda code, task_id, agent_label="": (
        calls.append("claim") or {"claimed": True, "status": "claimed",
                                  "confirmation": {"body": "任务原文", "untrusted": True}}
    ))
    monkeypatch.setattr("tools.llano_projects.confirm", lambda code, task_id: (
        calls.append("confirm") or {"status": "active"}
    ))

    body = client.post("/api/projects/demo/tasks/7/claim", json={"agent_label": "小助手"}).json()

    assert body["status"] == "claimed"
    assert body["confirmation"]["body"] == "任务原文"
    # 认领**没有**顺手开工。
    assert calls == ["claim"]


def test_confirm_passes_untrusted_through(client, monkeypatch):
    """`untrusted` 要跟着任务描述进入私聊上下文，不能因为『这是主人的会话』就清掉。"""
    monkeypatch.setattr("tools.llano_projects.confirm",
                        lambda code, task_id: {"status": "active", "untrusted": True})

    body = client.post("/api/projects/demo/tasks/7/confirm").json()

    assert body["untrusted"] is True


def test_there_is_no_claim_and_confirm_shortcut():
    """便利接口会被用，然后防线就没了 —— 所以它不能存在。"""
    paths = {getattr(route, "path", "") for route in web_server.app.routes}

    assert not any("claim-and-confirm" in p or "claim_and_confirm" in p for p in paths)


def test_api_key_never_reaches_the_renderer(client, monkeypatch):
    """渲染进程拿不到 Key 是整条通路的前提。指纹只回哈希前缀，且不进响应体。"""
    monkeypatch.setenv("LLANO_GATEWAY_API_KEY", "sk-super-secret-value")
    monkeypatch.setattr("tools.llano_projects.list_projects", lambda: [{"code": "demo"}])

    raw = client.get("/api/projects").text

    assert "sk-super-secret" not in raw


# ── 群聊 agent 的一轮（阶段 27-E）────────────────────────────────────────

def test_tick_runs_one_turn_with_the_configured_model(client, monkeypatch):
    seen = {}

    def fake_run_turn(**kwargs):
        seen.update(kwargs)

        return {"ran": True, "replied": True, "turn_id": 7}

    monkeypatch.setattr("tools.collab_agent_turn.run_turn", fake_run_turn)
    monkeypatch.setattr("tools.llano_projects.list_messages", lambda *a, **k: [])

    body = client.post("/api/projects/demo/agents/11/tick", json={"after_seq": 3}).json()

    assert body["ran"] is True
    assert seen["code"] == "demo" and seen["member_id"] == 11 and seen["after_seq"] == 3
    # 判定在网关：这一层不预判「有没有该回的事」，只负责跑。
    assert seen["reason"] == "mention"


def test_tick_reports_no_trigger_as_a_normal_result(client, monkeypatch):
    """`ran=false` 是正常返回。做成错误的话，客户端每次空转都要 catch 一下。"""
    monkeypatch.setattr("tools.collab_agent_turn.run_turn",
                        lambda **kw: {"ran": False, "reason": "no_trigger"})
    monkeypatch.setattr("tools.llano_projects.list_messages", lambda *a, **k: [])

    response = client.post("/api/projects/demo/agents/11/tick")

    assert response.status_code == 200
    assert response.json() == {"ran": False, "reason": "no_trigger"}


def test_tick_failure_is_loud(client, monkeypatch):
    def boom(**_kwargs):
        raise _unavailable("你不能触发这个 agent", status=403)

    monkeypatch.setattr("tools.collab_agent_turn.run_turn", boom)
    monkeypatch.setattr("tools.llano_projects.list_messages", lambda *a, **k: [])

    response = client.post("/api/projects/demo/agents/11/tick")

    assert response.status_code == 403
    assert "不能触发" in response.json()["detail"]


# ── 触发范围与活跃状态（阶段 27-E 接线）─────────────────────────────────

def test_respond_to_is_proxied_and_failures_are_loud(client, monkeypatch):
    seen = {}

    def fake(code, member_id, respond_to, allowlist=None):
        seen.update(code=code, member_id=member_id, respond_to=respond_to)

        return {"respond_to": respond_to, "allowlist": allowlist or []}

    monkeypatch.setattr("tools.llano_projects.set_respond_to", fake)

    body = client.put("/api/projects/demo/members/11/respond-to",
                      json={"respond_to": "nobody"}).json()

    assert body["respond_to"] == "nobody"
    assert seen == {"code": "demo", "member_id": 11, "respond_to": "nobody"}


def test_illegal_respond_to_is_refused_by_the_gateway(client, monkeypatch):
    """非法值由网关拒绝，sidecar 原样抛出去 —— 权限设置不能静默纠正。"""
    def boom(code, member_id, respond_to, allowlist=None):
        raise _unavailable("触发范围只能是 owner_only / allowlist / project_members / nobody", status=400)

    monkeypatch.setattr("tools.llano_projects.set_respond_to", boom)

    response = client.put("/api/projects/demo/members/11/respond-to", json={"respond_to": "everyone"})

    assert response.status_code == 400
    assert "触发范围" in response.json()["detail"]


def test_turns_read_degrades_instead_of_breaking_the_page(client, monkeypatch):
    """活跃状态取不到不影响聊天本身。"""
    monkeypatch.setattr("tools.llano_projects.list_turns", lambda code: {})

    body = client.get("/api/projects/demo/turns").json()

    assert body["turns"] == []
    assert body["hop_limit"] == 8


# ── 建项目与拉自己的 agent（2026-09-07）──────────────────────────────────
#
# 这两个动作原本只有网关后台有 —— 而后台的定位是看板。没有它们，桌面端连一个
# 自己的项目都建不出来，建出来了也没有 agent，而 agent 在群里说话正是这个功能
# 存在的理由。


def test_creating_a_project_is_proxied(client, monkeypatch):
    seen = {}

    def fake_create(code, name="", description=""):
        seen.update(code=code, name=name, description=description)
        return {"id": 3, "code": code}

    monkeypatch.setattr("tools.llano_projects.create_project", fake_create)

    body = client.post("/api/projects", json={"code": "q4", "name": "四季度"}).json()

    assert body["code"] == "q4"
    assert seen == {"code": "q4", "name": "四季度", "description": ""}


def test_creating_a_project_without_a_name_falls_back_to_the_code(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("tools.llano_projects.create_project",
                        lambda code, name="", description="": seen.update(name=name) or {"id": 1, "code": code})

    client.post("/api/projects", json={"code": "q4"})

    assert seen["name"] == "q4"


def test_an_empty_project_code_is_rejected_before_the_network(client):
    assert client.post("/api/projects", json={"code": "   "}).status_code == 400


def test_creating_a_project_reports_the_gateway_refusal(client, monkeypatch):
    """写失败必须响 —— 静默的话界面上是「建好了，但列表里没有」。"""
    def boom(code, name="", description=""):
        raise SkillHubUnavailable("项目标识已被占用")

    monkeypatch.setattr("tools.llano_projects.create_project", boom)

    response = client.post("/api/projects", json={"code": "q4"})

    assert response.status_code >= 400
    assert "项目标识已被占用" in response.json()["detail"]


def test_retrying_a_repository_is_proxied_as_a_write(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "tools.llano_projects.retry_repository",
        lambda code: seen.append(code) or {"status": "pending", "git_repo_url": ""},
    )

    body = client.post("/api/projects/q4/repository/retry").json()

    assert body["status"] == "pending"
    assert seen == ["q4"]


def test_pulling_my_agent_never_forwards_a_uid(client, monkeypatch):
    """只能拉自己的。服务端一律用调用者的 uid，客户端连字段都不该发。"""
    seen = {}
    monkeypatch.setattr("tools.llano_projects.pull_my_agent",
                        lambda code, label="": seen.update(code=code, label=label) or {"id": 7, "rejoined": False})

    body = client.post("/api/projects/q4/agents", json={"agent_label": "我的助手"}).json()

    assert body["id"] == 7
    assert seen == {"code": "q4", "label": "我的助手"}


def test_signing_out_drops_the_cached_group_token(client, monkeypatch):
    """群聊令牌缓存跟着身份走。退出之后不能把上一个人的凭据留在内存里等过期。"""
    from tools import llano_projects

    monkeypatch.setattr(llano_projects, "identity_fingerprint", lambda: "gw#test-account")
    calls = []
    monkeypatch.setattr(llano_projects, "_post",
                        lambda path, body=None: calls.append(path) or {"api_key": "sk-g", "expires_at": "2099-01-01T00:00:00"})

    llano_projects.group_session("q4")
    llano_projects.group_session("q4")
    assert len(calls) == 1

    client.post("/api/projects/session/reset")

    llano_projects.group_session("q4")
    assert len(calls) == 2


def test_successful_empty_projects_clear_previous_cache(client, monkeypatch):
    monkeypatch.setattr('tools.llano_projects.list_projects', lambda: [{'code': 'demo'}])
    assert client.get('/api/projects').json()['projects']
    monkeypatch.setattr('tools.llano_projects.list_projects', lambda: [])
    body = client.get('/api/projects').json()
    assert body['projects'] == []
    assert body['stale'] is False


def test_revoked_read_never_falls_back_to_cached_members(client, monkeypatch):
    from tools.llano_skill_hub import SkillHubUnavailable
    monkeypatch.setattr('tools.llano_projects.list_members', lambda code: [{'id': 1}])
    assert client.get('/api/projects/demo/members').json()['members']
    def revoked(code):
        error = SkillHubUnavailable('removed')
        error.status = 403
        raise error
    monkeypatch.setattr('tools.llano_projects.list_members', revoked)
    assert client.get('/api/projects/demo/members').status_code == 403
    assert router_mod._cached('members:demo') is None
