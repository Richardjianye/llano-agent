"""Codex 渠道核心（提取自 codex2api）的纯逻辑测试 + mock 上游端到端

覆盖四块：
1. 登录：PKCE / 授权码解析 / id_token 身份提取
2. 凭证：JSON 形态、新鲜度判定、坏凭证报错
3. 翻译：chat → responses 请求构建、Responses 事件 → chat chunk（含工具调用）
4. 连通：出站指纹、SSE 解析、mock 上游的流式/非流式全链路
"""

import base64
import hashlib
import json
import time

import httpx
import pytest

from services.codex_channel import oauth as codex_oauth
from services.codex_channel import upstream as codex_upstream
from services.codex_channel.credentials import (CodexCredential, CredentialError,
                                                load_credential)
from services.codex_channel.oauth import (AccountIdentity, TokenSet,
                                          build_auth_url, code_challenge_s256,
                                          extract_code, parse_id_token)
from services.codex_channel.translate import (StreamTranslator,
                                              build_responses_request,
                                              sanitize_tool_history,
                                              usage_to_chat)
from services.forwarding import UpstreamTarget, build_target


# ==================== 登录 ====================


def test_pkce_challenge_is_s256_of_verifier():
    verifier = "a" * 43
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert code_challenge_s256(verifier) == expected


def test_auth_url_carries_pkce_and_offline_scope():
    url = build_auth_url("http://localhost:1455/auth/callback", "st", "ver")
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    for part in ("code_challenge_method=S256", "client_id=app_EMoamEEZ73f0CkXaXp7hrann",
                 "offline_access", "codex_cli_simplified_flow=true", "state=st"):
        assert part in url


@pytest.mark.parametrize("raw,expected", [
    ("http://localhost:1455/auth/callback?code=abc&state=st", ("abc", "st")),
    ("code=abc&state=st", ("abc", "st")),
    ("justcode", ("justcode", "")),
])
def test_extract_code_forms(raw, expected):
    assert extract_code(raw) == expected


def _fake_jwt(payload: dict) -> str:
    def seg(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    return f"{seg(b'{}')}.{seg(json.dumps(payload).encode())}.sig"


def test_parse_id_token_extracts_identity():
    id_token = _fake_jwt({
        "email": "staff@corp.example",
        "sub": "user-1",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-9",
            "chatgpt_plan_type": "plus",
            "chatgpt_user_id": "user-1",
        },
    })
    identity = parse_id_token(id_token)
    assert identity.email == "staff@corp.example"
    assert identity.chatgpt_account_id == "acc-9"
    assert identity.plan_type == "plus"


# ==================== 凭证 ====================


def _cred(expiry_offset: float = 3600) -> CodexCredential:
    return CodexCredential(
        tokens=TokenSet(
            access_token="at", refresh_token="rt", id_token="idt",
            expires_at=time.time() + expiry_offset,
        ),
        identity=AccountIdentity(email="a@b.c", chatgpt_account_id="acc"),
    )


def test_credential_roundtrip_and_freshness():
    raw = _cred().to_json()
    back = CodexCredential.from_json(raw)
    assert back.tokens.refresh_token == "rt"
    assert back.identity.chatgpt_account_id == "acc"
    assert back.access_fresh() is True
    assert CodexCredential.from_json(_cred(expiry_offset=10).to_json()).access_fresh() is False


def test_credential_rejects_plain_api_key():
    with pytest.raises(CredentialError):
        CodexCredential.from_json("sk-something")
    with pytest.raises(CredentialError):
        load_credential(None)


# ==================== 请求翻译 ====================


def test_messages_become_responses_input():
    payload = {
        "model": "gpt-5.4-codex",
        "messages": [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{\"city\":\"BJ\"}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴"},
        ],
    }
    req = build_responses_request(payload)
    assert req["store"] is False and req["stream"] is True
    assert req["include"] == ["reasoning.encrypted_content"]
    assert req["instructions"] == ""  # Codex 要求该字段存在
    types = [item["type"] for item in req["input"]]
    # assistant 空文本不产生 message 项（codex2api 同口径），直接是 function_call
    assert types == ["message", "message", "function_call", "function_call_output"]
    assert req["input"][0]["role"] == "developer"
    assert req["input"][2]["name"] == "get_weather"
    assert req["input"][3]["call_id"] == "call_1"


def test_sampling_params_are_dropped():
    """Codex 后端不接受 temperature/top_p，发了会 400"""
    req = build_responses_request({
        "model": "m", "messages": [{"role": "user", "content": "x"}],
        "temperature": 0.7, "top_p": 0.9, "max_tokens": 100,
    })
    for banned in ("temperature", "top_p", "max_tokens", "stream_options"):
        assert banned not in req


def test_tools_and_tool_choice_flatten():
    req = build_responses_request({
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}
        }}],
        "tool_choice": {"type": "function", "function": {"name": "f"}},
    })
    assert req["tools"][0] == {"type": "function", "name": "f", "description": "d",
                               "parameters": {"type": "object"}}
    assert req["tool_choice"] == {"type": "function", "name": "f"}


def test_response_format_maps_to_text_format():
    req = build_responses_request({
        "model": "m", "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "out", "schema": {"type": "object"}, "strict": True,
        }},
    })
    assert req["text"]["format"]["type"] == "json_schema"
    assert req["text"]["format"]["name"] == "out"


def test_reasoning_effort_max_only_for_new_models():
    assert build_responses_request({"model": "gpt-5.6-codex", "messages": [],
                                    "reasoning_effort": "max"})["reasoning"]["effort"] == "max"
    assert build_responses_request({"model": "gpt-5.4-codex", "messages": [],
                                    "reasoning_effort": "max"})["reasoning"]["effort"] == "xhigh"


def test_sanitize_drops_broken_tool_call_with_its_output():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "bad", "type": "function",
             "function": {"name": "f", "arguments": "{broken"}},
        ]},
        {"role": "tool", "tool_call_id": "bad", "content": "orphan"},
        {"role": "user", "content": "hi"},
    ]
    cleaned = sanitize_tool_history(messages)
    assert [m["role"] for m in cleaned] == ["user"]
    req = build_responses_request({"model": "m", "messages": messages})
    assert all(i.get("call_id") != "bad" for i in req["input"])


def test_empty_tool_arguments_normalize_to_object():
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": ""}},
    ]}]
    cleaned = sanitize_tool_history(messages)
    assert cleaned[0]["tool_calls"][0]["function"]["arguments"] == "{}"


# ==================== 响应翻译 ====================


def test_stream_text_and_final_usage():
    tr = StreamTranslator("m", "chatcmpl-1", 123)
    chunk, terminal, failed = tr.translate({"type": "response.output_text.delta", "delta": "Hel"})
    assert chunk["choices"][0]["delta"] == {"content": "Hel"} and not terminal
    tr.translate({"type": "response.output_text.delta", "delta": "lo"})
    chunk, terminal, failed = tr.translate({
        "type": "response.completed",
        "response": {"usage": {
            "input_tokens": 10, "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 2},
            "input_tokens_details": {"cached_tokens": 3},
        }},
    })
    assert terminal and not failed
    assert chunk["choices"][0]["finish_reason"] == "stop"
    assert chunk["usage"]["prompt_tokens"] == 10
    assert chunk["usage"]["prompt_tokens_details"] == {"cached_tokens": 3}
    assert chunk["usage"]["completion_tokens_details"] == {"reasoning_tokens": 2}
    assert tr.final_response()["choices"][0]["message"]["content"] == "Hello"


def test_stream_tool_call_flow():
    tr = StreamTranslator("m", "chatcmpl-2", 123)
    chunk, _, _ = tr.translate({"type": "response.output_item.added", "output_index": 0,
                                "item": {"type": "function_call", "id": "item_1",
                                         "call_id": "call_9", "name": "get_weather"}})
    call = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert call == {"index": 0, "id": "call_9", "type": "function",
                    "function": {"name": "get_weather", "arguments": ""}}
    chunk, _, _ = tr.translate({"type": "response.function_call_arguments.delta",
                                "item_id": "item_1", "output_index": 0, "delta": "{\"city\":"})
    assert chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == "{\"city\":"
    tr.translate({"type": "response.function_call_arguments.done", "item_id": "item_1",
                  "output_index": 0, "arguments": "{\"city\": \"BJ\"}"})
    chunk, terminal, failed = tr.translate({
        "type": "response.completed",
        "response": {"usage": {"input_tokens": 1, "output_tokens": 2}},
    })
    assert terminal and chunk["choices"][0]["finish_reason"] == "tool_calls"
    final = tr.final_response()["choices"][0]["message"]["tool_calls"][0]
    assert final["function"]["arguments"] == "{\"city\": \"BJ\"}"
    assert final["id"] == "call_9"


def test_stream_incomplete_becomes_length():
    tr = StreamTranslator("m", "chatcmpl-3", 123)
    chunk, terminal, _ = tr.translate({
        "type": "response.incomplete",
        "response": {"incomplete_details": {"reason": "max_output_tokens"}, "usage": {}},
    })
    assert terminal and chunk["choices"][0]["finish_reason"] == "length"


def test_stream_failed_event_is_error_chunk():
    tr = StreamTranslator("m", "chatcmpl-4", 123)
    chunk, terminal, failed = tr.translate({
        "type": "response.failed",
        "response": {"error": {"message": "rate limited"}},
    })
    assert terminal and failed
    assert chunk["error"]["message"] == "rate limited"


def test_usage_to_chat_none_for_missing():
    assert usage_to_chat(None) is None
    assert usage_to_chat({"input_tokens": 4})["total_tokens"] == 4


# ==================== 连通 ====================


def test_resolve_base_url():
    assert codex_upstream.resolve_base_url("codex://chatgpt") == \
        "https://chatgpt.com/backend-api/codex"
    assert codex_upstream.resolve_base_url("codex://relay.corp") == \
        "https://relay.corp/backend-api/codex"
    assert codex_upstream.resolve_base_url("codex://relay.corp/codex") == \
        "https://relay.corp/codex"


def test_headers_match_codex_cli_identity():
    headers = codex_upstream.build_headers("at", "acc")
    assert headers["Authorization"] == "Bearer at"
    assert headers["Originator"] == "codex-tui"
    assert headers["User-Agent"].startswith("codex-tui/")
    assert headers["Version"] == codex_upstream.CODEX_CLIENT_VERSION
    assert headers["User-Agent"].startswith(
        f'{headers["Originator"]}/{headers["Version"]} '
    )
    assert f'({headers["Originator"]}; {headers["Version"]})' in headers["User-Agent"]
    assert headers["Chatgpt-Account-Id"] == "acc"
    assert headers["X-Codex-Beta-Features"] == "remote_compaction_v2"


async def test_parse_sse_events_handles_event_and_data_lines():
    async def lines():
        for line in [
            "event: response.output_text.delta",
            'data: {"type": "response.output_text.delta", "delta": "x"}',
            "",
            'data: {"type": "response.completed"}',
            "",
        ]:
            yield line

    events = [e async for e in codex_upstream.parse_sse_events(lines())]
    assert [e["type"] for e in events] == ["response.output_text.delta", "response.completed"]


# ==================== mock 上游端到端 ====================

SSE_BODY = (
    'event: response.output_text.delta\n'
    'data: {"type": "response.output_text.delta", "delta": "\u4f60\u597d"}\n\n'
    'event: response.completed\n'
    'data: {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 3}}}\n\n'
).encode("utf-8")


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_client(monkeypatch, handler):
    """把 codex 模块的出站 httpx 换成 MockTransport（真实请求管线，不发真网）"""
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), *args, **kwargs)
    monkeypatch.setattr(codex_upstream.httpx, "AsyncClient", factory)


async def test_model_probe_uses_current_identity_to_receive_new_models(monkeypatch):
    """Upstream gates its catalog on client version, not the account whitelist."""
    def handler(request):
        version = request.url.params["client_version"]
        assert request.headers["version"] == version
        assert request.headers["user-agent"].startswith(f"codex-tui/{version} ")
        assert f"(codex-tui; {version})" in request.headers["user-agent"]
        models = [{"slug": "gpt-5.6-luna"}]
        if int(version.split(".")[1]) >= 162:
            models.append({"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol"})
        return httpx.Response(200, json={"models": models})

    _patch_client(monkeypatch, handler)
    result = await codex_upstream.probe_models(_cred(), "codex://chatgpt")
    assert "gpt-6.1-sol" in result["models"]
    assert result["models"] == [item["slug"] for item in result["raw"]["models"]]


def _handler_factory(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=SSE_BODY)
    return handler


async def test_stream_chat_end_to_end(monkeypatch):
    captured = {}
    _patch_client(monkeypatch, _handler_factory(captured))
    cred = _cred()

    rows = []
    async for line, chunk in codex_upstream.stream_chat(
        cred, "codex://chatgpt",
        {"model": "gpt-5.4-codex", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        cache_key="sess-hash", timeout_seconds=5,
    ):
        rows.append((line, chunk))

    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["authorization"] == "Bearer at"
    assert captured["headers"]["session-id"] == "sess-hash"
    assert captured["body"]["model"] == "gpt-5.4-codex"
    assert captured["body"]["stream"] is True

    lines = [line for line, _ in rows]
    assert lines[0].startswith("data: {")
    chunks = [c for _, c in rows if c]
    assert chunks[0]["choices"][0]["delta"] == {"content": "你好"}
    assert chunks[-1]["usage"]["prompt_tokens"] == 7


async def test_complete_chat_end_to_end(monkeypatch):
    captured = {}
    _patch_client(monkeypatch, _handler_factory(captured))
    result = await codex_upstream.complete_chat(
        _cred(), "codex://chatgpt",
        {"model": "gpt-5.4-codex", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "你好"
    assert result["usage"]["completion_tokens"] == 3


async def test_upstream_401_maps_to_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="unauthorized")
    _patch_client(monkeypatch, handler)
    with pytest.raises(codex_upstream.CodexUpstreamError) as exc:
        await codex_upstream.complete_chat(_cred(), "codex://chatgpt", {"model": "m", "messages": []})
    assert exc.value.code == "codex_auth_failed"


# ==================== 转发层分流 ====================


class _FakeProvider:
    def __init__(self, base_url, api_key_encrypted="blob"):
        self.id = 1
        self.base_url = base_url
        self.api_key_encrypted = api_key_encrypted
        self.status = 1
        self.timeout_seconds = 60
        self.name = "t"


class _FakeModel:
    id = 2
    model_name = "gpt-5.4-codex"


def test_build_target_detects_codex_protocol(monkeypatch):
    monkeypatch.setattr("services.forwarding.decrypt_secret", lambda *_: "{}")
    target = build_target(_FakeProvider("codex://chatgpt"), _FakeModel(), session_id="sess")
    assert target.protocol == "codex"
    assert target.session_id == "sess"

    target = build_target(_FakeProvider("https://api.example.com"), _FakeModel())
    assert target.protocol == "openai"


def test_codex_cache_key_is_stable_hash_and_session_scoped():
    from services.forwarding import _codex_cache_key
    a = UpstreamTarget(1, "codex://chatgpt", "{}", 60, "m", 2, "codex", "sess-1")
    b = UpstreamTarget(1, "codex://chatgpt", "{}", 60, "m", 2, "codex", "sess-2")
    c = UpstreamTarget(1, "codex://chatgpt", "{}", 60, "m", 2, "codex", "sess-1")
    assert _codex_cache_key(a) == _codex_cache_key(c)
    assert _codex_cache_key(a) != _codex_cache_key(b)
    assert len(_codex_cache_key(a)) == 32
    assert "sess-1" not in _codex_cache_key(a)  # 原始会话号不出网关


# ==================== 转发分支端到端（chat.py 将走的路径） ====================


async def test_call_upstream_codex_branch_end_to_end(monkeypatch):
    """非流式：call_upstream 对 codex 目标返回 chat.completion JSON"""
    captured = {}
    _patch_client(monkeypatch, _handler_factory(captured))
    target = UpstreamTarget(
        provider_id=1, base_url="codex://chatgpt", api_key=_cred().to_json(),
        timeout_seconds=5, model_name="gpt-5.4-codex", model_id=2,
        protocol="codex", session_id="sess-77",
    )
    from services.forwarding import call_upstream
    result = await call_upstream(target, {"model": "gpt-5.4-codex",
                                          "messages": [{"role": "user", "content": "hi"}]})
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "你好"
    # 会话号以哈希形式进上游，原始值不出网关
    assert captured["body"]["prompt_cache_key"] != ""
    assert "sess-77" not in json.dumps(captured)


async def test_stream_upstream_codex_branch_end_to_end(monkeypatch):
    """流式：stream_upstream 对 codex 目标 yield 标准 data: 行"""
    captured = {}
    _patch_client(monkeypatch, _handler_factory(captured))
    target = UpstreamTarget(
        provider_id=1, base_url="codex://chatgpt", api_key=_cred().to_json(),
        timeout_seconds=5, model_name="gpt-5.4-codex", model_id=2,
        protocol="codex", session_id="",
    )
    from services.forwarding import parse_sse_line, stream_upstream
    lines = []
    chunks = []
    async for line, chunk in stream_upstream(target, {"model": "gpt-5.4-codex",
                                                      "messages": [{"role": "user", "content": "hi"}],
                                                      "stream": True}):
        lines.append(line)
        parsed = parse_sse_line(line)
        if parsed:
            chunks.append(parsed)
    assert all(line.startswith("data: ") for line in lines)
    assert chunks[0]["choices"][0]["delta"] == {"content": "你好"}
    assert chunks[-1]["usage"]["total_tokens"] == 10


# ==================== 剩余额度与掉号检测 ====================


def test_wham_usage_url_derivation():
    from services.codex_channel.upstream import wham_usage_url
    assert wham_usage_url("codex://chatgpt") == "https://chatgpt.com/backend-api/wham/usage"
    assert wham_usage_url("codex://relay/codex") == "https://relay/wham/usage"


def test_parse_wham_usage_windows_and_credits():
    from services.codex_channel.upstream import parse_wham_usage
    snap = parse_wham_usage({
        "email": "a@b.c", "plan_type": "pro",
        "rate_limit": {
            "allowed": True, "limit_reached": False,
            "primary_window": {"used_percent": 4.0, "limit_window_seconds": 604800,
                               "reset_after_seconds": 423223, "reset_at": 1792034355},
            "secondary_window": {"usedPercent": 12, "limitWindowSeconds": 18000,
                                 "resetAfterSeconds": 600},
        },
        "rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 0},
    })
    assert snap["plan_type"] == "pro"
    assert snap["primary_window"]["window_seconds"] == 604800
    assert snap["secondary_window"]["used_percent"] == 12.0  # 两种拼写都认
    assert snap["secondary_window"]["window_seconds"] == 18000
    assert snap["reset_credits"]["available_count"] == 3
    assert snap["limit_reached"] is False


def test_parse_wham_usage_empty():
    from services.codex_channel.upstream import parse_wham_usage
    snap = parse_wham_usage({})
    assert snap["allowed"] is True and snap["primary_window"] is None


def test_health_marking_and_snapshot():
    from services.codex_channel import health
    health._health.clear()
    change = health.mark(1, health.STATE_DEAD, "invalid_grant")
    assert change is None  # 首次记录不算变化
    snap = health.snapshot(1)
    assert snap["state"] == "dead" and snap["reason"] == "invalid_grant"
    change = health.mark(1, health.STATE_ALIVE)
    assert change == "dead → alive"
    assert health.snapshot(1)["state"] == "alive"
    health._health.clear()


def test_health_classify():
    from services.codex_channel import health
    from services.codex_channel.oauth import OAuthError
    from services.codex_channel.upstream import CodexUpstreamError
    assert health._classify(OAuthError("x")) == health.STATE_UNKNOWN
    assert health._classify(CodexUpstreamError("x", 401, "codex_auth_failed")) == health.STATE_DEAD
    assert health._classify(CodexUpstreamError("x", 502, "connect_failed")) == health.STATE_UNKNOWN
    assert health._classify(RuntimeError("x")) == health.STATE_UNKNOWN


async def test_truncated_upstream_sse_is_not_reported_as_success(monkeypatch):
    def handler(req):
        return httpx.Response(200, content=b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n', headers={"content-type":"text/event-stream"})
    _patch_client(monkeypatch, handler)
    with pytest.raises(codex_upstream.CodexUpstreamError, match="完成事件前中断"):
        await codex_upstream.complete_chat(_cred(), "codex://chatgpt", {"model":"m","messages":[]})
    with pytest.raises(codex_upstream.CodexUpstreamError, match="完成事件前中断"):
        async for _ in codex_upstream.stream_chat(_cred(), "codex://chatgpt", {"model":"m","messages":[]}):
            pass


async def test_failed_sse_reaches_forwarding_as_failure(monkeypatch):
    def handler(req):
        return httpx.Response(200, content=b'data: {"type":"response.failed","response":{"error":{"message":"failed"}}}\n\n', headers={"content-type":"text/event-stream"})
    _patch_client(monkeypatch, handler)
    with pytest.raises(codex_upstream.CodexUpstreamError):
        async for _ in codex_upstream.stream_chat(_cred(), "codex://chatgpt", {"model":"m","messages":[]}):
            pass


async def test_token_endpoint_errors_do_not_echo_credentials(monkeypatch):
    async def handler(req):
        return httpx.Response(400, json={"error":"invalid_grant", "error_description":"test-refresh-secret", "access_token":"test-access-secret"})
    _patch_client(monkeypatch, handler)
    with pytest.raises(codex_oauth.OAuthLoginRequired) as caught:
        await codex_oauth.refresh_tokens("test-refresh-secret")
    assert "test-refresh-secret" not in str(caught.value) and "test-access-secret" not in str(caught.value)
