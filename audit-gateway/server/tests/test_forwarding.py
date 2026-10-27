"""转发链路的纯逻辑测试

覆盖三块容易出错的地方：
1. 转发前的请求体改写（模型名替换、强制 include_usage）
2. SSE 解析与用量提取（流式统计全靠这个）
3. 发送方向脱敏
"""

import pytest
from services.forwarding import (UpstreamTarget, collect_delta, extract_text,
                                 parse_sse_line, prepare_payload)
from services.masking import mask_messages, mask_text
from services.usage import estimate_tokens, extract_usage

TARGET = UpstreamTarget(
    provider_id=1,
    base_url="https://example.com",
    api_key="sk-upstream",
    timeout_seconds=60,
    model_name="deepseek-chat",
    model_id=9,
)


# ==================== 请求体改写 ====================

def test_alias_is_replaced_with_upstream_model_name():
    """客户端传的是别名，发给上游必须换成真实模型名"""
    payload = prepare_payload({"model": "公司通用模型", "messages": []}, TARGET)
    assert payload["model"] == "deepseek-chat"


def test_stream_forces_include_usage():
    """流式不带 include_usage 的话最后一个 chunk 没有 usage，token 数就丢了"""
    payload = prepare_payload({"model": "x", "stream": True}, TARGET)
    assert payload["stream_options"]["include_usage"] is True


def test_stream_options_preserves_client_fields():
    payload = prepare_payload(
        {"model": "x", "stream": True, "stream_options": {"foo": 1}}, TARGET
    )
    assert payload["stream_options"] == {"foo": 1, "include_usage": True}


def test_non_stream_does_not_add_stream_options():
    payload = prepare_payload({"model": "x"}, TARGET)
    assert "stream_options" not in payload


def test_client_cannot_override_gateway_fields():
    """客户端塞 api_key / base_url 想改上游？丢掉"""
    payload = prepare_payload(
        {"model": "x", "api_key": "sk-evil", "base_url": "http://evil"}, TARGET
    )
    assert "api_key" not in payload
    assert "base_url" not in payload


def test_prepare_payload_does_not_mutate_input():
    body = {"model": "x", "stream": True}
    prepare_payload(body, TARGET)
    assert body["model"] == "x"
    assert "stream_options" not in body


# ==================== SSE 解析 ====================

def test_parse_sse_line():
    assert parse_sse_line('data: {"a": 1}') == {"a": 1}


@pytest.mark.parametrize("line", [
    "data: [DONE]",
    "data:",
    ": keep-alive",
    "event: message",
    "data: 不是 json",
    'data: "字符串不是对象"',
])
def test_parse_sse_line_ignores_non_data(line):
    assert parse_sse_line(line) is None


def test_collect_delta():
    chunk = {"choices": [{"delta": {"content": "你好"}}]}
    assert collect_delta(chunk) == "你好"


def test_collect_delta_tolerates_empty_chunks():
    """首个 chunk 通常只有 role，没有 content"""
    assert collect_delta({"choices": [{"delta": {"role": "assistant"}}]}) == ""
    assert collect_delta({"choices": []}) == ""
    assert collect_delta({}) == ""


def test_extract_text_from_completion():
    payload = {"choices": [{"message": {"content": "回复内容"}}]}
    assert extract_text(payload) == "回复内容"


# ==================== 用量提取 ====================

def test_extract_usage():
    usage = extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15}})
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_extract_usage_computes_total_when_missing():
    usage = extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert usage["total_tokens"] == 15


def test_extract_usage_returns_none_without_usage():
    """没有 usage 时必须返回 None，调用方据此打「估算」标记"""
    assert extract_usage({}) is None
    assert extract_usage({"usage": None}) is None
    assert extract_usage({"usage": {}}) is None


def test_estimate_tokens_is_never_zero_for_text():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") >= 1
    assert estimate_tokens("测试" * 100) > 0


# ==================== 脱敏 ====================

@pytest.fixture
def masking(monkeypatch):
    import config
    from services import masking as masking_module

    config.get_settings.cache_clear()
    monkeypatch.setenv("MASK_KEYWORDS", "LLANO Agent,LLANO Agent,LLANO")
    monkeypatch.setenv("MASK_PLACEHOLDER", "[公司]")
    masking_module.reset_cache()

    yield masking_module

    config.get_settings.cache_clear()
    masking_module.reset_cache()


def test_mask_replaces_keyword(masking):
    text, hit = mask_text("我们LLANO Agent今年的营收")
    assert text == "我们[公司]今年的营收"
    assert hit is True


def test_longer_keyword_wins(masking):
    """「LLANO Agent」不能被「LLANO Agent」先吃掉一半，留下孤零零的「AI工作台」"""
    text, _ = mask_text("LLANO Agent的产品线")
    assert text == "[公司]的产品线"


def test_mask_is_case_insensitive(masking):
    text, hit = mask_text("llano 的内部系统")
    assert hit is True
    assert "llano" not in text.lower()


def test_mask_reports_no_hit(masking):
    text, hit = mask_text("一段普通的话")
    assert hit is False
    assert text == "一段普通的话"


def test_mask_messages_handles_multimodal(masking):
    """图片等结构原样保留 —— 图片脱敏已明确放弃（决策 #9）"""
    messages = [
        {"role": "user", "content": "LLANO Agent的数据"},
        {"role": "user", "content": [
            {"type": "text", "text": "LLANO 的图"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]},
    ]
    masked, hit = mask_messages(messages)

    assert hit is True
    assert masked[0]["content"] == "[公司]的数据"
    assert masked[1]["content"][0]["text"] == "[公司] 的图"
    assert masked[1]["content"][1]["image_url"]["url"] == "https://x/y.png"
    # 原始消息不能被就地改写
    assert messages[0]["content"] == "LLANO Agent的数据"


def test_mask_messages_redacts_safe_text_attachment_but_not_complex_file(masking):
    import base64

    text_data = base64.b64encode("LLANO 内部说明".encode()).decode()
    pdf_data = "JVBERi0xLjQ="
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "file",
                    "file": {
                        "filename": "notes.txt",
                        "file_data": f"data:text/plain;charset=utf-8;base64,{text_data}",
                    },
                },
                {
                    "type": "file",
                    "file": {
                        "filename": "report.pdf",
                        "file_data": f"data:application/pdf;base64,{pdf_data}",
                    },
                },
            ],
        }
    ]

    masked, hit = mask_messages(messages)

    masked_text = masked[0]["content"][0]["file"]["file_data"].split(",", 1)[1]
    assert base64.b64decode(masked_text).decode() == "[公司] 内部说明"
    assert masked[0]["content"][1]["file"]["file_data"] == f"data:application/pdf;base64,{pdf_data}"
    assert hit is True
    assert "LLANO" in base64.b64decode(text_data).decode()
    assert messages[0]["content"][0]["file"]["file_data"].endswith(text_data)


def test_mask_disabled_when_no_keywords(monkeypatch):
    """没配关键词时原样透传，不该报错"""
    import config
    from services import masking as masking_module

    config.get_settings.cache_clear()
    monkeypatch.setenv("MASK_KEYWORDS", "")
    masking_module.reset_cache()

    text, hit = mask_text("LLANO Agent")
    assert text == "LLANO Agent"
    assert hit is False

    config.get_settings.cache_clear()
    masking_module.reset_cache()


# ── 上游地址拼接（2026-09-11 线上 404 的根因）────────────────────────────

def test_chat_url_appends_v1_for_bare_host():
    """只登记到域名的供应商（DeepSeek / 自家网关）仍要补 /v1"""
    from services.forwarding import chat_completions_url

    assert (
        chat_completions_url("https://api.deepseek.com")
        == "https://api.deepseek.com/v1/chat/completions"
    )
    # 末尾斜杠不该多出一段空路径
    assert (
        chat_completions_url("https://models.example.invalid/")
        == "https://models.example.invalid/v1/chat/completions"
    )


def test_chat_url_keeps_existing_version_segment():
    """base_url 自带版本段就不再补 /v1

    火山方舟是 `…/api/v3`，旧写法拼成 `/api/v3/v1/chat/completions`，
    上游一律 404 —— 客户端只看到「上游返回 404」，看不出是路径错了。
    """
    from services.forwarding import chat_completions_url

    assert (
        chat_completions_url("https://ark.cn-beijing.volces.com/api/v3")
        == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    )
    assert (
        chat_completions_url("https://proxy.example.com/openai/v1/")
        == "https://proxy.example.com/openai/v1/chat/completions"
    )
    # `/api` 不是版本段，仍然要补
    assert (
        chat_completions_url("https://proxy.example.com/api")
        == "https://proxy.example.com/api/v1/chat/completions"
    )


# ── 别名不许带空格（同一次事故的另一半）──────────────────────────────

def test_model_alias_rejects_whitespace():
    """客户端按空格分词，别名带空格会让所有人切不了模型"""
    import pytest
    from pydantic import ValidationError

    from model.request.org import ModelCreate, ModelUpdate

    with pytest.raises(ValidationError):
        ModelUpdate(alias="豆包 Seed2.0-Lite")
    with pytest.raises(ValidationError):
        ModelCreate(provider_id=1, model_name="doubao x", display_name="豆包")

    # 中文展示名照常，别名去空格后可用；留空表示不设别名
    assert ModelUpdate(alias="豆包Seed2.0-Lite").alias == "豆包Seed2.0-Lite"
    assert ModelUpdate(alias="").alias is None
    assert (
        ModelCreate(
            provider_id=1, model_name=" doubao-seed-2-0-lite-260428 ", display_name="豆包 Seed2.0 Lite"
        ).model_name
        == "doubao-seed-2-0-lite-260428"
    )


def test_clearing_alias_is_distinguishable_from_not_touching_it():
    """「清空别名」必须真的清空

    管理端点清空后弹「模型已更新」而别名还在，原因是 DAO 把 None 当成
    「不改动」——于是这个动作根本表达不出来。和单价一样用 UNSET 哨兵区分。
    """
    import inspect

    from db.updates.catalog import UNSET, update_model

    assert inspect.signature(update_model).parameters["alias"].default is UNSET

    src = inspect.getsource(update_model)
    assert "if alias is not UNSET:" in src


def test_model_update_route_passes_alias_null_through():
    """路由层要按「字段在不在请求体里」判断，而不是看值是不是 None"""
    import inspect

    from router.admin import catalog

    src = inspect.getsource(catalog.model_update)
    assert 'alias=(body.alias if "alias" in fields else UNSET)' in src

    from model.request.org import ModelUpdate

    # 显式传 null → 出现在 model_fields_set 里 → 清空
    assert "alias" in ModelUpdate(alias=None).model_fields_set
    # 不传 → 不在 → 不改动
    assert "alias" not in ModelUpdate(display_name="豆包").model_fields_set
