"""群聊 agent 租约接口上的判定 —— 阶段 27-E

数据库那几件事（唯一索引互斥、水位 UPDATE、租约回收）压在真实行锁上，喂假
session 测出来的绿是假的 —— 与 test_project_collaboration 同一处理，留给 e2e。
这里测的是**判定与推导**：

1. mention 命中要同时对上主人与 agent 名（一个人可以有多个 agent）
2. 坏掉的配置一律失败关闭，不换来更松的权限
3. 记账维度从**凭据**推，不听请求头
4. `X-Llano-Turn-Id` 只是个号码：服务端自己查租约，查不到就退回普通口径
"""

from __future__ import annotations

import types

import pytest
from db.selects.project_turn import _mentions_agent
from router.v1 import chat as chat_router
from router.v1.project_turns import _allowlist_of
from services import conversation_scope as cs
from services import project_runtime as rt

OWNER = "uid-owner"


class _Caller:
    def __init__(self, scope=cs.PRIVATE, uid=OWNER, project_id=None):
        self.uid = uid
        self.scope = scope
        self.scope_project_id = project_id


def _row(mentions_json):
    return types.SimpleNamespace(mentions_json=mentions_json)


def _agent(label="小助手", uid=OWNER):
    return {"id": 11, "project_id": 1, "member_kind": "agent", "user_uid": uid,
            "agent_label": label, "status": "active"}


# ── mention 命中 ──────────────────────────────────────────────────────────

def test_mention_matches_owner_and_label_together():
    """基线：@ 的确实是这个 agent。"""
    row = _row('[{"kind": "agent", "uid": "uid-owner", "label": "小助手"}]')

    assert _mentions_agent(row, _agent()) is True


def test_same_owner_different_agent_is_not_a_hit():
    """一个人可以有多个 agent。只对 uid 不对名字的话，@ 甲的助手会把乙的也叫醒。"""
    row = _row('[{"kind": "agent", "uid": "uid-owner", "label": "另一个"}]')

    assert _mentions_agent(row, _agent()) is False


def test_mentioning_a_person_does_not_wake_their_agent():
    row = _row('[{"kind": "user", "uid": "uid-owner", "label": "阿甲"}]')

    assert _mentions_agent(row, _agent()) is False


@pytest.mark.parametrize("payload", [None, "", "не json", '{"kind": "agent"}', "[1, 2, 3]"])
def test_broken_mentions_json_is_treated_as_no_mention(payload):
    """坏掉的一行不该让整个项目的触发判定挂掉，也不该被当成「@ 了所有人」。"""
    assert _mentions_agent(_row(payload), _agent()) is False


# ── 白名单解析失败关闭 ────────────────────────────────────────────────────

def test_allowlist_parses_a_normal_list():
    assert _allowlist_of({"respond_allowlist": '["u-1", "u-2"]'}) == ["u-1", "u-2"]


@pytest.mark.parametrize("raw", [None, "", "not json", '{"u": 1}', "123"])
def test_broken_allowlist_is_empty_not_everyone(raw):
    """解析不了的配置不该换来更松的权限。"""
    assert _allowlist_of({"respond_allowlist": raw}) == []


def test_allowlist_mode_with_a_broken_list_only_leaves_the_owner():
    agent = dict(_agent(), respond_to=rt.RESPOND_ALLOWLIST, respond_allowlist="not json")
    mate = {"id": 22, "project_id": 1, "member_kind": "user", "user_uid": "uid-mate", "status": "active"}
    owner = {"id": 23, "project_id": 1, "member_kind": "user", "user_uid": OWNER, "status": "active"}

    assert rt.may_trigger(agent, mate, allowlist=_allowlist_of(agent)) is False
    assert rt.may_trigger(agent, owner, allowlist=_allowlist_of(agent)) is True


# ── 记账维度（P0-E）────────────────────────────────────────────────────────

def test_group_credential_bills_as_project():
    assert chat_router._billing_dimensions(_Caller(cs.GROUP)) == {
        "call_kind": "chat", "trigger_source": "project"
    }


def test_private_credential_bills_as_private():
    assert chat_router._billing_dimensions(_Caller(cs.PRIVATE))["trigger_source"] == "private"


def test_billing_dimension_ignores_request_headers():
    """「算个人还是算协作」不能是客户端的自选项 —— 报表要靠这一维拆开两种消耗。

    `_billing_dimensions` 的签名里根本没有请求这个入参，这条断言守的就是它。
    """
    import inspect

    assert list(inspect.signature(chat_router._billing_dimensions).parameters) == ["caller"]


# ── X-Llano-Turn-Id 只是个号码 ────────────────────────────────────────────

class _Request:
    def __init__(self, turn_id=None):
        self.headers = {"X-Llano-Turn-Id": str(turn_id)} if turn_id is not None else {}


def _patch_turn(monkeypatch, turn):
    import db.inserts.project_turn as turns

    async def fake_get_turn(turn_id):
        return turn

    monkeypatch.setattr(turns, "get_turn", fake_get_turn)


async def test_turn_request_id_comes_from_the_lease(monkeypatch):
    """记账用的是**租约上的** id，不是头里那串字符。"""
    _patch_turn(monkeypatch, {
        "id": 7, "project_id": 1, "owner_uid": OWNER,
        "status": rt.TURN_RUNNING, "trigger_request_id": "turn-abc",
    })

    rid = await chat_router._turn_request_id(_Request(7), _Caller(cs.GROUP, project_id=1))

    assert rid == "turn-abc"


async def test_turn_id_from_another_owner_is_ignored(monkeypatch):
    """拿别人的 turn 号来记账 —— 那笔账会挂到别人头上。"""
    _patch_turn(monkeypatch, {
        "id": 7, "project_id": 1, "owner_uid": "someone-else",
        "status": rt.TURN_RUNNING, "trigger_request_id": "turn-abc",
    })

    assert await chat_router._turn_request_id(_Request(7), _Caller(cs.GROUP, project_id=1)) == ""


async def test_turn_id_from_another_project_is_ignored(monkeypatch):
    """拿项目 A 的群聊令牌去记项目 B 的账。"""
    _patch_turn(monkeypatch, {
        "id": 7, "project_id": 2, "owner_uid": OWNER,
        "status": rt.TURN_RUNNING, "trigger_request_id": "turn-abc",
    })

    assert await chat_router._turn_request_id(_Request(7), _Caller(cs.GROUP, project_id=1)) == ""


async def test_finished_turn_does_not_lend_its_request_id(monkeypatch):
    _patch_turn(monkeypatch, {
        "id": 7, "project_id": 1, "owner_uid": OWNER,
        "status": rt.TURN_COMPLETED, "trigger_request_id": "turn-abc",
    })

    assert await chat_router._turn_request_id(_Request(7), _Caller(cs.GROUP, project_id=1)) == ""


async def test_private_credential_cannot_borrow_a_turn_id(monkeypatch):
    """私聊凭据带上一个 turn 号，不该换来群聊那套记账口径。"""
    _patch_turn(monkeypatch, {
        "id": 7, "project_id": 1, "owner_uid": OWNER,
        "status": rt.TURN_RUNNING, "trigger_request_id": "turn-abc",
    })

    assert await chat_router._turn_request_id(_Request(7), _Caller(cs.PRIVATE)) == ""


@pytest.mark.parametrize("raw", ["", "abc", "  "])
async def test_garbage_turn_header_falls_back_quietly(monkeypatch, raw):
    """头里写什么都不该换一个记账口径，更不该让这次对话失败。"""
    _patch_turn(monkeypatch, None)
    request = _Request()
    request.headers = {"X-Llano-Turn-Id": raw}

    assert await chat_router._turn_request_id(request, _Caller(cs.GROUP, project_id=1)) == ""
