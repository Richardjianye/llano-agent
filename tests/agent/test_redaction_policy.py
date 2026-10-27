from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.redaction_policy import (
    preload_agent_redaction_policy,
    RedactionPolicyCache,
    RedactionPolicyUnavailable,
    resolve_product_redaction_policy,
)


NOW = datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)


def _payload(*, expires_in: int = 300, version: str = "sha256:v1") -> dict:
    expires_at = NOW + timedelta(seconds=expires_in)
    return {
        "object": "llano.redaction_policy",
        "version": version,
        "keywords": ["示例公司", "LLANO"],
        "placeholder": "[公司]",
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def test_stock_hermes_does_not_fetch_product_policy():
    fetches = []

    policy = resolve_product_redaction_policy(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="secret",
        product_policy=None,
        cache=RedactionPolicyCache(),
        fetch_json=lambda *_args, **_kwargs: fetches.append(True),
        now=NOW,
    )

    assert policy is None
    assert fetches == []


def test_invalid_managed_product_never_disables_redaction(monkeypatch):
    from hermes_cli.product_policy import ProductPolicyState, ProductPolicyStatus

    monkeypatch.setattr(
        "hermes_cli.product_policy.load_product_policy_status",
        lambda: ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="config unreadable",
        ),
    )

    with pytest.raises(RedactionPolicyUnavailable) as raised:
        resolve_product_redaction_policy(
            provider="custom",
            base_url="https://personal.example/v1",
            api_key="secret",
            cache=RedactionPolicyCache(),
            fetch_json=lambda *_args: pytest.fail("must not fetch"),
            now=NOW,
        )

    assert raised.value.reason == "managed-product-policy-invalid"


def test_managed_provider_fetches_with_bearer_and_reuses_valid_cache():
    calls = []

    def fetch_json(url, headers):
        calls.append((url, headers))
        return _payload()

    product = SimpleNamespace(provider_id="llano-gateway", gateway_origin="https://gateway.example")
    cache = RedactionPolicyCache()

    first = resolve_product_redaction_policy(
        provider="llano-gateway",
        base_url="https://gateway.example/v1",
        api_key="device-secret",
        product_policy=product,
        cache=cache,
        fetch_json=fetch_json,
        now=NOW,
    )
    second = resolve_product_redaction_policy(
        provider="llano-gateway",
        base_url="https://gateway.example/v1",
        api_key="device-secret",
        product_policy=product,
        cache=cache,
        fetch_json=fetch_json,
        now=NOW + timedelta(seconds=30),
    )

    assert first == second
    assert calls == [
        (
            "https://gateway.example/v1/redaction-policy",
            {"Accept": "application/json", "Authorization": "Bearer device-secret"},
        )
    ]


@pytest.mark.parametrize("refresh_failure", [OSError("offline"), _payload(expires_in=-1)])
def test_valid_cache_survives_refresh_failure_but_expired_cache_fails_closed(refresh_failure):
    product = SimpleNamespace(provider_id="llano-gateway", gateway_origin="https://gateway.example")
    cache = RedactionPolicyCache(refresh_skew_seconds=60)
    calls = 0

    def fetch_json(_url, _headers):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _payload(expires_in=90)
        if isinstance(refresh_failure, Exception):
            raise refresh_failure
        return refresh_failure

    first = resolve_product_redaction_policy(
        provider="llano-gateway",
        base_url="https://gateway.example/v1",
        api_key="device-secret",
        product_policy=product,
        cache=cache,
        fetch_json=fetch_json,
        now=NOW,
    )

    still_valid = resolve_product_redaction_policy(
        provider="llano-gateway",
        base_url="https://gateway.example/v1",
        api_key="device-secret",
        product_policy=product,
        cache=cache,
        fetch_json=fetch_json,
        now=NOW + timedelta(seconds=45),
    )

    assert still_valid == first

    with pytest.raises(RedactionPolicyUnavailable, match="已过期") as raised:
        resolve_product_redaction_policy(
            provider="llano-gateway",
            base_url="https://gateway.example/v1",
            api_key="device-secret",
            product_policy=product,
            cache=cache,
            fetch_json=fetch_json,
            now=NOW + timedelta(seconds=91),
            sleep=lambda _seconds: None,
        )

    # 分类而不是文案：aux_redaction 靠 reason 决定要不要改判成"不许离开网关"，
    # 以前它匹配的是消息文本，改文案就会悄悄改掉路由
    assert raised.value.reason == "cache-expired"


def test_agent_preload_uses_resolved_runtime_identity(monkeypatch):
    calls = []
    expected = object()

    monkeypatch.setattr(
        "agent.redaction_policy.resolve_product_redaction_policy",
        lambda **kwargs: calls.append(kwargs) or expected,
    )
    agent = SimpleNamespace(
        provider="llano-gateway",
        base_url="https://gateway.example/v1",
        api_key="device-secret",
    )

    assert preload_agent_redaction_policy(agent) is expected
    assert calls == [
        {
            "provider": "llano-gateway",
            "base_url": "https://gateway.example/v1",
            "api_key": "device-secret",
        }
    ]


# ── 托管端点识别（阶段 4 · CP3 发现）────────────────────────────────────
# 上游 resolve_* 在有 base_url 时会把非内置 provider 的身份塌缩成 "custom"
# （get_provider 只认内置），导致品牌客户端 agent init 直接 fail-closed。
# 端点没变，只是标签丢了 —— 这组用例钉住"按端点认、而不是无条件信任 custom"。

from types import SimpleNamespace  # noqa: E402

from agent.redaction_policy import _is_managed_endpoint  # noqa: E402


def _policy(origin="http://127.0.0.1:7899", provider_id="llano-gateway"):
    return SimpleNamespace(provider_id=provider_id, gateway_origin=origin)


def test_managed_endpoint_accepts_exact_provider_id():
    assert _is_managed_endpoint("llano-gateway", "", _policy()) is True
    assert _is_managed_endpoint("LLANO-Gateway", "", _policy()) is True


def test_managed_endpoint_accepts_collapsed_custom_on_gateway_base_url():
    """provider 被上游改名成 custom，但端点仍是公司网关 —— 必须放行。

    这正是品牌桌面端的真实运行态；不放行的话客户端连天都聊不了。
    """
    policy = _policy()
    assert _is_managed_endpoint("custom", "http://127.0.0.1:7899/v1", policy) is True
    assert _is_managed_endpoint("custom", "http://127.0.0.1:7899", policy) is True
    assert _is_managed_endpoint("custom:llano", "http://127.0.0.1:7899/v1", policy) is True


def test_managed_endpoint_rejects_custom_pointing_elsewhere():
    """放宽的是"标签"，不是"端点"。

    少了这条，改动就从"认回被改名的自家网关"变成"任何自定义端点都算自家"，
    脱敏策略会被发往任意地址 —— 那是把决策 #9 的防线直接拆掉。
    """
    policy = _policy()
    assert _is_managed_endpoint("custom", "https://evil.example.com/v1", policy) is False
    assert _is_managed_endpoint("custom", "", policy) is False
    # 前缀相近但不同 origin，不能被 startswith 蒙混过去
    assert _is_managed_endpoint("custom", "http://127.0.0.1:78990/v1", policy) is False


def test_managed_endpoint_rejects_other_named_providers():
    policy = _policy()
    assert _is_managed_endpoint("openrouter", "http://127.0.0.1:7899/v1", policy) is False
    assert _is_managed_endpoint("anthropic", "http://127.0.0.1:7899/v1", policy) is False


def test_managed_endpoint_requires_configured_origin():
    """网关地址没配（test/prod 环境目前留空）时不能瞎认。"""
    assert _is_managed_endpoint("custom", "http://127.0.0.1:7899/v1", _policy(origin="")) is False
