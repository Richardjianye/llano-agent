import json
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from services.audit_content import CONTENT_MAX_CHARS
from services.audit_envelope import (AuditEnvelopeError, extract_audit_envelope,
                                     extract_audit_messages)
from utils.gateway_auth import Caller


def test_private_envelope_is_removed_and_supplies_original_audit_messages():
    body = {
        "model": "flash",
        "messages": [{"role": "user", "content": "[公司]的计划"}],
        "_llano_audit": {
            "policy_version": "sha256:v1",
            "messages": [{"role": "user", "content": "LLANO Agent的计划"}],
        },
    }

    audit_messages = extract_audit_messages(body)

    assert audit_messages == [{"role": "user", "content": "LLANO Agent的计划"}]
    assert "_llano_audit" not in body
    assert body["messages"] == [{"role": "user", "content": "[公司]的计划"}]


def test_missing_envelope_copies_received_messages_before_server_fallback():
    body = {"messages": [{"role": "user", "content": "LLANO Agent"}]}
    before = deepcopy(body["messages"])

    audit_messages = extract_audit_messages(body)
    body["messages"][0]["content"] = "[公司]"

    assert audit_messages == before


@pytest.mark.parametrize(
    "envelope",
    [
        [],
        {"policy_version": "", "messages": []},
        {"policy_version": "sha256:v1", "messages": "not-a-list"},
        {"policy_version": "sha256:v1", "messages": []},
    ],
)
def test_malformed_private_envelope_is_rejected_after_removal(envelope):
    body = {
        "messages": [{"role": "user", "content": "hello"}],
        "_llano_audit": envelope,
    }

    with pytest.raises(AuditEnvelopeError):
        extract_audit_messages(body)

    assert "_llano_audit" not in body


def test_oversized_private_envelope_is_rejected_after_removal(monkeypatch):
    from services import audit_envelope

    monkeypatch.setattr(audit_envelope, "MAX_AUDIT_ENVELOPE_BYTES", 32)
    body = {
        "messages": [{"role": "user", "content": "safe"}],
        "_llano_audit": {
            "policy_version": "sha256:v1",
            "messages": [{"role": "user", "content": "x" * 100}],
        },
    }

    with pytest.raises(AuditEnvelopeError, match="大小限制"):
        extract_audit_messages(body)

    assert "_llano_audit" not in body


# ==================== 会话串联字段（决策 #52） ====================


def test_session_fields_are_carried_when_the_client_supplies_them():
    body = {
        "messages": [{"role": "user", "content": "[公司]的计划"}],
        "_llano_audit": {
            "policy_version": "sha256:v1",
            "messages": [{"role": "user", "content": "LLANO Agent的计划"}],
            "session_id": "20260821_151706_ac236f",
            "turn_kind": "title",
        },
    }

    envelope = extract_audit_envelope(body)

    assert envelope.session_id == "20260821_151706_ac236f"
    assert envelope.turn_kind == "title"


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "   ",
        "has space",
        "../../etc/passwd",
        "a'; DROP TABLE conversation_logs; --",
        "%E4%B8%AD",
        "x" * 65,
    ],
)
def test_untrusted_session_ids_degrade_to_null_instead_of_failing_the_call(value):
    """串联字段不合规就降级，**不报错**。

    它们只影响审计页怎么分组。让一次正常的对话因为会话 ID 里多了个点号
    而整个失败，是把「看得更清楚」这件事的代价转嫁给用户。
    """
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "_llano_audit": {
            "policy_version": "sha256:v1",
            "messages": [{"role": "user", "content": "hi"}],
            "session_id": value,
        },
    }

    envelope = extract_audit_envelope(body)

    assert envelope.session_id is None
    assert envelope.messages == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("chat", "chat"),
        ("CHAT", "chat"),
        (" approval ", "approval"),
        ("title", "title"),
        ("other", "other"),
        ("anything_else", "other"),
        ("", "other"),
        (None, "other"),
        (42, "other"),
    ],
)
def test_turn_kind_is_a_whitelist_not_whatever_the_client_typed(value, expected):
    """客户端字符串不能把这一列变成无界枚举。"""
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "_llano_audit": {
            "policy_version": "sha256:v1",
            "messages": [{"role": "user", "content": "hi"}],
            "turn_kind": value,
        },
    }

    assert extract_audit_envelope(body).turn_kind == expected


def test_calls_without_an_envelope_default_to_chat_so_they_stay_visible():
    """没信封的调用（stock 客户端、绕过我们客户端的直连）默认落 chat。

    `other` 那一档在列表页默认隐藏。把来路不明的调用默认藏起来，
    等于把最该被看见的那类调用藏了 —— 方向正好反了。
    """
    envelope = extract_audit_envelope({"messages": [{"role": "user", "content": "LLANO Agent"}]})

    assert envelope.session_id is None
    assert envelope.turn_kind == "chat"


async def test_chat_sends_only_redacted_copy_upstream_and_records_original(monkeypatch):
    import config
    from loguru import logger
    from router.v1 import chat
    from services import masking

    config.get_settings.cache_clear()
    monkeypatch.setenv("MASK_KEYWORDS", "LLANO Agent")
    monkeypatch.setenv("MASK_PLACEHOLDER", "[公司]")
    masking.reset_cache()

    class Request:
        state = SimpleNamespace(request_id="audit-envelope-test")

        async def json(self):
            return {
                "model": "flash",
                "messages": [{"role": "user", "content": "[公司]的计划"}],
                "_llano_audit": {
                    "policy_version": "sha256:v1",
                    "messages": [{"role": "user", "content": "LLANO Agent的计划"}],
                    "session_id": "20260821_151706_ac236f",
                    "turn_kind": "chat",
                },
            }

    model = SimpleNamespace(
        id=9,
        alias="flash",
        model_name="upstream-flash",
        provider_id=3,
        billing_mode="token",
        input_price_per_1m=None,
        output_price_per_1m=None,
        cached_input_price_per_1m=None,
        price_per_call=None,
        currency="CNY",
    )
    target = SimpleNamespace(
        provider_id=3,
        model_name="upstream-flash",
        model_id=9,
        base_url="https://upstream.example",
        api_key="upstream-secret",
        timeout_seconds=30,
    )
    upstream_payloads = []
    recorded = []
    log_lines = []
    sink_id = logger.add(lambda message: log_lines.append(str(message)), level="DEBUG")

    async def call_upstream(_target, payload):
        upstream_payloads.append(deepcopy(payload))
        return {
            "choices": [{"message": {"content": "收到"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }

    monkeypatch.setattr(chat, "check_model_permission", lambda *_args: _async_value(model))
    monkeypatch.setattr(chat, "applicable_quota_statuses", lambda *_args: _async_value([]))
    monkeypatch.setattr("db.selects.catalog.get_provider", lambda *_args: _async_value(SimpleNamespace(id=3)))
    monkeypatch.setattr(chat, "build_target", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(chat, "call_upstream", call_upstream)
    monkeypatch.setattr(chat, "record_usage", lambda facts: _record_async(recorded, facts))

    try:
        response = await chat.chat_completions(
            Request(),
            Caller(uid="u1", username="员工", role="employee", dept_id=1, dept_code="rd"),
        )
    finally:
        logger.remove(sink_id)

    assert response.status_code == 200
    assert upstream_payloads[0]["messages"] == [{"role": "user", "content": "[公司]的计划"}]
    assert "_llano_audit" not in upstream_payloads[0]
    assert recorded[0].request_messages == [{"role": "user", "content": "LLANO Agent的计划"}]
    # 串联字段要一路走到写库的 facts 上，而不是在路由里被摘掉就丢了
    assert recorded[0].session_id == "20260821_151706_ac236f"
    assert recorded[0].turn_kind == "chat"
    assert "20260821_151706_ac236f" not in str(upstream_payloads[0])
    assert "LLANO Agent" not in response.body.decode("utf-8")
    assert "LLANO Agent" not in "".join(log_lines)
    assert "_llano_audit" not in "".join(log_lines)

    config.get_settings.cache_clear()
    masking.reset_cache()


async def test_usage_rows_never_serialize_private_audit_original(monkeypatch):
    """Usage inserts contain metrics only; originals belong to conversation audit."""
    import db
    from services.usage import UsageFacts, _write_usage

    statements = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement, *_args):
            statements.append(statement)
            return SimpleNamespace(inserted_primary_key=[])

        async def commit(self):
            return None

    class SessionMaker:
        def __call__(self):
            return Session()

    async def init_session():
        return SessionMaker()

    monkeypatch.setattr(db, "init_async_session", init_session)
    sentinel = "LLANO Agent-AUDIT-ONLY-STAGE3"
    await _write_usage(
        UsageFacts(
            request_id="usage-no-audit-leak",
            user_uid="u1",
            request_messages=[{"role": "user", "content": sentinel}],
        ),
        date(2026, 8, 18),
    )

    assert len(statements) == 2
    usage_values = statements[0].compile().params
    assert sentinel not in str(usage_values)
    assert "_llano_audit" not in str(usage_values)


async def _async_value(value):
    return value


async def _record_async(items, value):
    items.append(value)


# ==================== 写入侧那条路（决策 #52 的内容模型） ====================


async def test_conversation_row_carries_the_new_content_model(monkeypatch):
    """⚠️ 这条打的是 `_write_conversation` 本身，不是它调用的那些纯函数。

    上面那些用例把 `record_usage` 整个 mock 掉了 —— 于是「整形算得对」全绿，
    而「整形结果有没有真的写进那一行」一个字没测。2026-08-20 和 08-21 连栽
    两次的就是这个形状（见 TODO「第 1 格测错了故障」「同一句英文漏了两次」）：
    **测函数不等于测到那条路。**
    """
    import db
    from services.usage import UsageFacts, _write_conversation

    inserted = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement, *_args):
            inserted.append(statement)
            return SimpleNamespace(inserted_primary_key=[1])

        async def commit(self):
            return None

    async def init_session():
        return SimpleNamespace(__call__=Session)

    class SessionMaker:
        def __call__(self):
            return Session()

    monkeypatch.setattr(db, "init_async_session", lambda: _async_value(SessionMaker()))

    await _write_conversation(
        UsageFacts(
            request_id="content-model",
            user_uid="u1",
            session_id="20260821_151706_ac236f",
            turn_kind="chat",
            request_messages=[
                {"role": "system", "content": "You are Hermes Agent. " + "细则。" * 8000},
                {"role": "user", "content": "列一下桌面文件"},
                {"role": "assistant",
                 "tool_calls": [{"id": "c1", "function": {"name": "list_dir"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "内部报价表.xlsx\n" * 300},
                {"role": "user", "content": "第二个文件是什么"},
            ],
            response_text="第二个是一份报价表。",
        )
    )

    values = inserted[0].compile().params

    # 问了什么：全文，且没被 20000 字的系统提示挤掉
    assert values["user_input"] == "第二个文件是什么"
    # 系统提示：只有指纹和字数
    assert len(values["system_prompt_sha256"]) == 64
    assert values["system_prompt_chars"] > CONTENT_MAX_CHARS
    assert "细则。" not in str(values)
    # 工具链：只有骨架，正文一个字不留
    assert json.loads(values["tool_skeleton"]) == [
        {"name": "list_dir", "chars": len("内部报价表.xlsx\n" * 300)}
    ]
    assert "内部报价表" not in str(values)
    # 串联字段一路走到这一行
    assert values["session_id"] == "20260821_151706_ac236f"
    assert values["turn_kind"] == "chat"
