import base64
from copy import deepcopy
from datetime import datetime, timezone

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.redaction_payload import (attach_audit_envelope,
                                     prepare_agent_redacted_request,
                                     prepare_redacted_request)
from agent.redaction_policy import RedactionPolicy


POLICY = RedactionPolicy(
    version="sha256:v1",
    keywords=("示例公司", "示例公司", "llano"),
    placeholder="[公司]",
    expires_at=datetime(2026, 8, 18, 6, 0, tzinfo=timezone.utc),
)


def _data_url(mime: str, text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"data:{mime};charset=utf-8;base64,{encoded}"


def _decode_data_url(value: str) -> str:
    return base64.b64decode(value.split(",", 1)[1]).decode("utf-8")


def test_prepare_redacts_supported_text_without_mutating_local_history():
    image = "data:image/png;base64,iVBORw0KGgo="
    request = {
        "model": "flash",
        "messages": [
            {"role": "user", "content": "示例公司和 LLANO"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "示例公司项目"},
                    {"type": "image_url", "image_url": {"url": image}},
                    {
                        "type": "file",
                        "file": {
                            "filename": "notes.txt",
                            "file_data": _data_url("text/plain", "LLANO 内部说明"),
                        },
                    },
                    {
                        "type": "file",
                        "file": {
                            "filename": "report.pdf",
                            "file_data": "data:application/pdf;base64,JVBERi0xLjQ=",
                        },
                    },
                ],
            },
        ],
    }
    before = deepcopy(request)

    prepared = prepare_redacted_request(request, POLICY)

    assert request == before
    assert prepared.payload is not request
    assert prepared.payload["messages"][0]["content"] == "[公司]和 [公司]"
    parts = prepared.payload["messages"][1]["content"]
    assert parts[0]["text"] == "[公司]项目"
    assert parts[1]["image_url"]["url"] == image
    assert _decode_data_url(parts[2]["file"]["file_data"]) == "[公司] 内部说明"
    assert parts[3]["file"]["file_data"] == "data:application/pdf;base64,JVBERi0xLjQ="
    assert prepared.audit_messages == before["messages"]


def test_longest_keyword_wins_case_insensitively():
    prepared = prepare_redacted_request(
        {"messages": [{"role": "user", "content": "示例公司 LLANO llano"}]},
        POLICY,
    )

    assert prepared.payload["messages"][0]["content"] == "[公司] [公司] [公司]"


def test_audit_envelope_is_added_only_to_a_fresh_extra_body():
    prepared = prepare_redacted_request(
        {"messages": [{"role": "user", "content": "示例公司"}], "extra_body": {"metadata": {"x": 1}}},
        POLICY,
    )

    outbound = attach_audit_envelope(prepared)

    assert "_llano_audit" not in prepared.payload["extra_body"]
    assert outbound["extra_body"]["metadata"] == {"x": 1}
    assert outbound["extra_body"]["_llano_audit"] == {
        "policy_version": "sha256:v1",
        "messages": [{"role": "user", "content": "示例公司"}],
        "turn_kind": "chat",
    }


# ==================== 会话串联字段（决策 #52） ====================


def test_envelope_carries_the_session_id_so_one_turn_can_be_reassembled():
    prepared = prepare_redacted_request(
        {"messages": [{"role": "user", "content": "示例公司"}]},
        POLICY,
        session_id="20260821_151706_ac236f",
        turn_kind="title",
    )

    envelope = attach_audit_envelope(prepared)["extra_body"]["_llano_audit"]

    assert envelope["session_id"] == "20260821_151706_ac236f"
    assert envelope["turn_kind"] == "title"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "20260821_151706 ac236f",       # 空格
        "../../etc/passwd",             # 路径
        "20260821'; DROP TABLE --",     # 注入形状
        "x" * 65,                       # 超长
    ],
)
def test_unusable_session_ids_are_dropped_rather_than_transported(value):
    """串不起来是正常状态，把垃圾发出去不是。

    网关侧还会独立校验一遍（services/audit_envelope.py）—— 这里归一化只是
    「别发垃圾」，不是安全边界：谁都能手写一个 body 打过来。
    """
    prepared = prepare_redacted_request(
        {"messages": [{"role": "user", "content": "示例公司"}]},
        POLICY,
        session_id=value,
    )

    assert prepared.session_id is None
    # 缺省时整个键都不出现，而不是发一个 null 过去
    assert "session_id" not in attach_audit_envelope(prepared)["extra_body"]["_llano_audit"]


def test_unknown_turn_kinds_collapse_to_other_instead_of_growing_the_enum():
    prepared = prepare_redacted_request(
        {"messages": [{"role": "user", "content": "示例公司"}]},
        POLICY,
        turn_kind="whatever_the_client_felt_like",
    )

    assert prepared.turn_kind == "other"


def test_agent_session_id_reaches_the_envelope_without_new_plumbing():
    """Hermes 自己就有会话 ID，不用新造标识 —— 直接从 agent 上读。"""
    agent = SimpleNamespace(
        provider="openai",
        base_url="https://gateway.example.com/v1",
        api_key="sk-test",
        session_id="20260821_151706_ac236f",
    )

    with patch(
        "agent.redaction_policy.resolve_product_redaction_policy",
        return_value=POLICY,
    ):
        prepared = prepare_agent_redacted_request(
            agent, {"messages": [{"role": "user", "content": "示例公司"}]}
        )

    assert prepared is not None
    assert prepared.session_id == "20260821_151706_ac236f"
    assert prepared.turn_kind == "chat"


@pytest.mark.parametrize("keywords", [("llano",), ()])
def test_redact_content_preserves_attachment_shapes_and_original(keywords):
    from dataclasses import replace
    from agent.redaction_payload import redact_content

    policy = replace(POLICY, keywords=keywords)
    content = [
        {"type": "text", "text": "LLANO picture"},
        {"type": "input_image", "image_url": "https://example.invalid/LLANO.png"},
        {"type": "image", "source": {"type": "base64", "data": "LLANO"}},
        {"type": "file", "file": {"file_data": _data_url("text/plain", "LLANO report")}},
    ]
    original = deepcopy(content)
    result = redact_content(content, policy)
    expected_word = "[公司]" if keywords else "LLANO"
    assert result[0]["text"] == f"{expected_word} picture"
    assert result[1:3] == original[1:3]
    assert _decode_data_url(result[3]["file"]["file_data"]) == f"{expected_word} report"
    assert content == original
    assert result is not content
