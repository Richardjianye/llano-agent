from enum import Enum
from types import SimpleNamespace

import pytest

from hermes_cli import product_policy
from hermes_cli.managed_scope import ManagedConfigState, ManagedConfigStatus


def _managed_policy(**overrides):
    product = {
        "enabled": True,
        "brand_id": "llano-ai-workbench",
        "environment": "development",
        "provider_id": "llano-gateway",
        "gateway_origin": "http://127.0.0.1:8000",
        "update_manifest_url": "http://127.0.0.1:8000/client/updates/manifest",
        "allowed_messaging_platforms": ["telegram", "weixin", "dingtalk"],
    }
    product.update(overrides)
    return {"product": product}


def _patch_managed(monkeypatch, config):
    state = ManagedConfigState.VALID if config else ManagedConfigState.ABSENT
    monkeypatch.setattr(
        product_policy,
        "load_managed_config_status",
        lambda: ManagedConfigStatus(state, config, None),
    )
    monkeypatch.setattr(product_policy, "load_managed_config", lambda: config)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"display": {"skin": "managed"}}, product_policy.ProductPolicyState.INVALID),
        ({"product": {}}, product_policy.ProductPolicyState.INVALID),
        ({"product": {"enabled": False}}, product_policy.ProductPolicyState.STOCK),
        (
            {"product": {"enabled": True, "brand_id": "brand-only"}},
            product_policy.ProductPolicyState.INVALID,
        ),
    ],
)
def test_product_policy_tri_state_does_not_collapse_broken_managed_config(
    monkeypatch, config, expected
):
    _patch_managed(monkeypatch, config)

    assert product_policy.load_product_policy_status().state is expected


def test_product_policy_status_propagates_managed_loader_failure(monkeypatch):
    monkeypatch.setattr(
        product_policy,
        "load_managed_config_status",
        lambda: ManagedConfigStatus(
            ManagedConfigState.INVALID,
            {},
            None,
            "permission denied",
        ),
    )

    status = product_policy.load_product_policy_status()

    assert status.state is product_policy.ProductPolicyState.INVALID
    assert status.reason == "permission denied"


def test_invalid_managed_policy_keeps_sensitive_boundaries_closed(monkeypatch):
    monkeypatch.setattr(
        product_policy,
        "load_managed_config_status",
        lambda: ManagedConfigStatus(
            ManagedConfigState.INVALID,
            {},
            None,
            "bad yaml",
        ),
    )
    unsafe = {
        "model": {
            "provider": "personal",
            "base_url": "https://personal.example/v1",
            "default": "private/model",
        },
        "providers": {"personal": {"api_key": "secret"}},
        "custom_providers": {"personal": {"base_url": "https://personal"}},
        "fallback_model": {"provider": "personal"},
        "image_gen": {"provider": "personal"},
        "nous": {"enabled": True},
        "pet": {"generation_provider": "personal", "scale": 3},
    }

    assert product_policy.capability_allowed("provider_management") is False
    assert product_policy.capability_allowed("nous_services") is False
    assert product_policy.capability_allowed(product_policy.WINDOW_ENUMERATION) is False
    assert product_policy.capability_allowed(
        product_policy.THIRD_PARTY_SKILL_SOURCES
    ) is False
    assert product_policy.config_write_allowed("model.provider", "personal") is False
    assert product_policy.messaging_platform_allowed("telegram") is False
    assert product_policy.model_switch_allowed() is False
    assert product_policy.model_switch_allowed("personal") is False
    with pytest.raises(product_policy.ProductCapabilityUnavailable):
        product_policy.require_capability("provider_management")

    safe = product_policy.sanitize_product_config(unsafe)
    assert safe["model"] == {}
    assert safe["providers"] == {}
    assert safe["custom_providers"] == {}
    assert safe["fallback_model"] == {}
    assert safe["image_gen"] == {}
    assert safe["nous"] == {}
    assert safe["pet"] == {"scale": 3}


def test_stock_hermes_remains_unrestricted(monkeypatch):
    _patch_managed(monkeypatch, {})

    assert product_policy.load_product_policy() is None
    assert product_policy.messaging_platform_allowed("discord") is True
    assert product_policy.model_switch_allowed() is True


def test_branded_policy_allows_only_configured_messaging_platforms(monkeypatch):
    _patch_managed(monkeypatch, _managed_policy())

    policy = product_policy.load_product_policy()

    assert policy is not None
    assert policy.provider_id == "llano-gateway"
    assert product_policy.messaging_platform_allowed("Telegram") is True
    assert product_policy.messaging_platform_allowed("weixin") is True
    assert product_policy.messaging_platform_allowed("dingtalk") is True
    assert product_policy.messaging_platform_allowed("discord") is False
    assert product_policy.model_switch_allowed() is True
    assert product_policy.model_switch_allowed("llano-gateway") is True
    assert product_policy.model_switch_allowed("openrouter") is False


@pytest.mark.parametrize("allowed", [None, []])
def test_unrestricted_messaging_keeps_other_product_boundaries(monkeypatch, allowed):
    config = _managed_policy(
        disabled_capabilities=["nous_services", "provider_management"]
    )
    if allowed is None:
        config["product"].pop("allowed_messaging_platforms")
    else:
        config["product"]["allowed_messaging_platforms"] = allowed
    _patch_managed(monkeypatch, config)

    for platform in ("feishu", "wecom", "slack", "discord", "new-plugin-channel"):
        assert product_policy.messaging_platform_allowed(platform)
    assert not product_policy.capability_allowed("nous_services")
    assert not product_policy.capability_allowed("provider_management")
    assert not product_policy.model_switch_allowed("openrouter")


def test_malformed_messaging_allowlist_does_not_open_managed_policy(monkeypatch):
    _patch_managed(monkeypatch, _managed_policy(allowed_messaging_platforms="discord"))
    assert product_policy.load_product_policy_status().state is product_policy.ProductPolicyState.INVALID
    assert not product_policy.messaging_platform_allowed("discord")


def test_unrestricted_messaging_preserves_enabled_and_disabled_adapters(monkeypatch):
    _patch_managed(monkeypatch, _managed_policy(allowed_messaging_platforms=[]))

    class Platform(Enum):
        SLACK = "slack"
        DISCORD = "discord"

    config = SimpleNamespace(platforms={
        Platform.SLACK: SimpleNamespace(enabled=True),
        Platform.DISCORD: SimpleNamespace(enabled=False),
    })
    product_policy.apply_messaging_policy(config)
    assert config.platforms[Platform.SLACK].enabled is True
    assert config.platforms[Platform.DISCORD].enabled is False


def test_branded_policy_disables_existing_blocked_adapters(monkeypatch):
    _patch_managed(monkeypatch, _managed_policy())
    class Platform(Enum):
        TELEGRAM = "telegram"
        DISCORD = "discord"

    telegram = Platform.TELEGRAM
    discord = Platform.DISCORD
    config = SimpleNamespace(
        platforms={
            telegram: SimpleNamespace(enabled=True),
            discord: SimpleNamespace(enabled=True),
        }
    )

    product_policy.apply_messaging_policy(config)

    assert config.platforms[telegram].enabled is True
    assert config.platforms[discord].enabled is False


def test_branded_policy_disables_only_configured_capabilities(monkeypatch):
    _patch_managed(
        monkeypatch,
        _managed_policy(
            disabled_capabilities=["nous_services", "pet_ai_generation"]
        ),
    )

    policy = product_policy.load_product_policy()

    assert policy is not None
    assert policy.disabled_capabilities == frozenset(
        {"nous_services", "pet_ai_generation"}
    )
    assert product_policy.capability_allowed("nous_services") is False
    assert product_policy.capability_allowed("pet_ai_generation") is False
    assert product_policy.capability_allowed("provider_management") is True
    assert product_policy.capability_allowed("mcp") is True


def test_stock_hermes_allows_every_capability(monkeypatch):
    _patch_managed(monkeypatch, {})

    assert product_policy.capability_allowed("nous_services") is True
    assert product_policy.capability_allowed("pet_ai_generation") is True


def test_require_capability_exposes_stable_reason(monkeypatch):
    _patch_managed(
        monkeypatch,
        _managed_policy(disabled_capabilities=["provider_oauth"]),
    )

    with pytest.raises(product_policy.ProductCapabilityUnavailable) as exc_info:
        product_policy.require_capability("provider_oauth")

    assert exc_info.value.capability == "provider_oauth"
    assert exc_info.value.reason == "capability_unavailable"
    assert str(exc_info.value) == "capability_unavailable"


def test_sanitize_product_config_preserves_unrelated_settings(monkeypatch):
    _patch_managed(
        monkeypatch,
        _managed_policy(
            disabled_capabilities=[
                "custom_model_providers",
                "provider_management",
                "moa",
                "builtin_image_generation",
                "pet_ai_generation",
                "nous_services",
            ]
        ),
    )
    original = {
        "display": {"theme": "light"},
        "terminal": {"timeout": 42},
        "model": {
            "provider": "openrouter",
            "default": "anthropic/claude",
            "base_url": "https://example.invalid/v1",
        },
            "providers": {"openrouter": {"api": "https://example.invalid/v1"}},
            "custom_providers": {"legacy": {"base_url": "https://legacy.invalid"}},
        "fallback_model": {"provider": "anthropic", "model": "claude"},
        "moa": {"enabled": True},
        "image_gen": {"provider": "fal"},
        "pet": {"generation_provider": "fal", "scale": 3},
        "nous": {"portal": True},
    }

    sanitized = product_policy.sanitize_product_config(original)

    assert sanitized["display"] == {"theme": "light"}
    assert sanitized["terminal"] == {"timeout": 42}
    assert sanitized["model"] == {
        "provider": "llano-gateway",
        "default": "anthropic/claude",
    }
    assert sanitized["providers"] == {}
    assert sanitized["custom_providers"] == {}
    assert sanitized["fallback_model"] == {}
    assert sanitized["moa"] == {}
    assert sanitized["image_gen"] == {}
    assert sanitized["pet"] == {"scale": 3}
    assert sanitized["nous"] == {}
    assert original["model"]["provider"] == "openrouter"
    assert original["providers"] != {}


@pytest.mark.parametrize(
    ("path", "capability"),
    [
        ("providers.openrouter", "custom_model_providers"),
        ("custom_providers.legacy", "custom_model_providers"),
        ("model.provider", "provider_management"),
        ("fallback_model.provider", "provider_management"),
        ("moa.enabled", "moa"),
        ("image_gen.provider", "builtin_image_generation"),
        ("pet.generation_provider", "pet_ai_generation"),
        ("nous.portal", "nous_services"),
        ("env.OPENROUTER_API_KEY", "provider_management"),
        ("env.NOUS_API_KEY", "nous_services"),
    ],
)
def test_config_write_allowed_rejects_blocked_capability_paths(
    monkeypatch, path, capability
):
    _patch_managed(
        monkeypatch,
        _managed_policy(disabled_capabilities=[capability]),
    )

    assert product_policy.config_write_allowed(path, "unsafe") is False


def test_config_write_allowed_preserves_unrelated_preferences(monkeypatch):
    _patch_managed(
        monkeypatch,
        _managed_policy(
            disabled_capabilities=[
                "provider_management",
                "custom_model_providers",
                "moa",
                "builtin_image_generation",
                "pet_ai_generation",
                "nous_services",
            ]
        ),
    )

    assert product_policy.config_write_allowed("display.theme", "light") is True
    assert product_policy.config_write_allowed("terminal.timeout", 42) is True
    assert product_policy.config_write_allowed("accessibility.reduce_motion", True) is True
    assert product_policy.config_write_allowed("updates.channel", "stable") is True


# ==================== 界面默认语言 ====================
#
# 这一组钉的是一个装到员工机器上才会暴露的问题：品牌构建全新安装后界面是英文的，
# 而切语言的开关在登录之后 —— 员工看不懂界面，也走不到那个开关。
#
# 根因不在客户端：load_config() 从 DEFAULT_CONFIG 起步，display.language 恒为
# "en"，所以「没设过」在合并结果里跟「设成英文」长得一样。


def _raw_config(monkeypatch, raw):
    """替掉 read_user_config_raw —— 它是 product_policy 里按函数体 import 的。"""
    import hermes_cli.config as config_module

    monkeypatch.setattr(config_module, "read_user_config_raw", lambda *a, **k: raw)


def test_language_default_fills_in_when_user_never_chose(monkeypatch):
    _patch_managed(
        monkeypatch,
        _managed_policy(default_language="zh"),
    )
    _raw_config(monkeypatch, {"terminal": {"cwd": "/home"}})

    # 合并过默认值的配置：display.language 在，值是 DEFAULT_CONFIG 的 "en"
    result = product_policy.apply_product_language_default(
        {"display": {"language": "en", "skin": "default"}}
    )

    assert result["display"]["language"] == "zh"
    # 同级键不能被顺手抹掉
    assert result["display"]["skin"] == "default"


def test_language_default_does_not_override_user_choice(monkeypatch):
    _patch_managed(
        monkeypatch,
        _managed_policy(default_language="zh"),
    )
    # 用户自己写进 config.yaml 的英文 —— 语言开关必须还管用
    _raw_config(monkeypatch, {"display": {"language": "en"}})

    result = product_policy.apply_product_language_default({"display": {"language": "en"}})

    assert result["display"]["language"] == "en"


def test_language_default_yields_to_managed_pin(monkeypatch):
    """管理员钉过就听管理员的 —— managed 覆盖层本来就盖在用户配置之上。"""
    managed = _managed_policy(default_language="zh")
    managed["display"] = {"language": "en"}
    _patch_managed(monkeypatch, managed)
    _raw_config(monkeypatch, {})

    result = product_policy.apply_product_language_default({"display": {"language": "en"}})

    assert result["display"]["language"] == "en"


def test_language_default_is_noop_for_stock_hermes(monkeypatch):
    _patch_managed(monkeypatch, {})
    _raw_config(monkeypatch, {})

    config = {"display": {"language": "en"}}
    result = product_policy.apply_product_language_default(config)

    assert result["display"]["language"] == "en"
    assert result is config  # 非品牌构建原样返回，不做多余的深拷贝


def test_language_default_is_noop_when_product_did_not_set_one(monkeypatch):
    """default_language 留空 = 不干预。品牌构建也不是都想换语言的。"""
    _patch_managed(monkeypatch, _managed_policy())
    _raw_config(monkeypatch, {})

    result = product_policy.apply_product_language_default({"display": {"language": "en"}})

    assert result["display"]["language"] == "en"


def test_unreadable_user_config_leaves_language_alone(monkeypatch):
    """读不到用户配置时保守处理：宁可保持英文，也不猜。"""
    _patch_managed(
        monkeypatch,
        _managed_policy(default_language="zh"),
    )

    import hermes_cli.config as config_module

    def _boom(*a, **k):
        raise OSError("config.yaml is a directory")

    monkeypatch.setattr(config_module, "read_user_config_raw", _boom)

    result = product_policy.apply_product_language_default({"display": {"language": "en"}})

    assert result["display"]["language"] == "en"
