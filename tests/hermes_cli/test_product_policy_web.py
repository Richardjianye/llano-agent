import asyncio

import pytest

from hermes_cli import product_policy
from hermes_cli import web_server as ws


def _patch_managed(monkeypatch, module, factory):
    """Branded/stock fixture for both managed-config seams.

    ``load_product_policy`` now reads the tri-state ``load_managed_config_status``
    so a broken managed install fails closed instead of looking stock; other
    helpers still read the legacy dict loader. Patch both from one factory.
    """
    from pathlib import Path

    from hermes_cli.managed_scope import ManagedConfigState, ManagedConfigStatus

    def status():
        config = factory()
        if not config:
            return ManagedConfigStatus(ManagedConfigState.ABSENT, {}, None)
        return ManagedConfigStatus(ManagedConfigState.VALID, config, Path("/managed"))

    monkeypatch.setattr(module, "load_managed_config", factory)
    monkeypatch.setattr(module, "load_managed_config_status", status)


def _branded_policy(*capabilities: str):
    return {
        "product": {
            "enabled": True,
            "brand_id": "llano-ai-workbench",
            "provider_id": "llano-gateway",
            "disabled_capabilities": list(capabilities),
            "allowed_messaging_platforms": ["telegram", "weixin", "dingtalk"],
        }
    }


@pytest.fixture
def branded(monkeypatch):
    _patch_managed(monkeypatch, product_policy, lambda: _branded_policy(
            "provider_management",
            "provider_oauth",
            "custom_model_providers",
            "nous_services",
        ))


def test_structured_config_rejects_only_blocked_fields_before_save(branded, monkeypatch):
    saves = []
    monkeypatch.setattr(ws, "read_raw_config", lambda: {"display": {"theme": "dark"}})
    monkeypatch.setattr(ws, "save_config", lambda config: saves.append(config))

    with pytest.raises(ws.HTTPException) as exc_info:
        asyncio.run(
            ws.update_config(
                ws.ConfigUpdate(config={"model": {"provider": "openrouter"}})
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "capability_unavailable"
    assert saves == []

    result = asyncio.run(
        ws.update_config(ws.ConfigUpdate(config={"display": {"theme": "light"}}))
    )
    assert result == {"ok": True}
    assert saves[0]["display"] == {"theme": "light"}
    assert saves[0]["providers"] == {}
    assert saves[0]["fallback_model"] == {}


def test_unchanged_blocked_field_rides_along_without_blocking_the_save(branded, monkeypatch):
    """2026-09-20 现场：用户切界面语言收到 capability_unavailable。

    每个设置界面都是「读回整份配置 → 改一个字段 → 整份提交」。品牌装机的
    config.yaml 里有 `providers: {llano-gateway: {}}`（网关给它指派模型时写的），
    于是每次保存都带着一个受管制字段，被判成越权写入 —— 连语言都切不了。
    原样带回一个没变的值是空操作，不是提权。
    """
    disk = {
        "display": {"language": "en"},
        "providers": {"llano-gateway": {}},
        "model": {"default": "deepseek-v4.1-flash", "provider": "llano-gateway"},
    }
    saves = []
    monkeypatch.setattr(ws, "read_raw_config", lambda: dict(disk))
    monkeypatch.setattr(ws, "save_config", lambda config: saves.append(config))

    result = asyncio.run(
        ws.update_config(
            ws.ConfigUpdate(
                config={
                    "display": {"language": "zh"},
                    "providers": {"llano-gateway": {}},
                    "model": "deepseek-v4.1-flash",
                }
            )
        )
    )

    assert result == {"ok": True}
    assert saves[0]["display"]["language"] == "zh"


def test_changing_a_blocked_field_is_still_rejected(branded, monkeypatch):
    """放行的只是「没变」，改动本身照旧拒绝 —— 否则这道闸门等于没有。"""
    disk = {"display": {"language": "en"}, "providers": {"llano-gateway": {}}}
    saves = []
    monkeypatch.setattr(ws, "read_raw_config", lambda: dict(disk))
    monkeypatch.setattr(ws, "save_config", lambda config: saves.append(config))

    with pytest.raises(ws.HTTPException) as exc_info:
        asyncio.run(
            ws.update_config(
                ws.ConfigUpdate(
                    config={
                        "display": {"language": "zh"},
                        "providers": {"llano-gateway": {"api": "https://evil.example/v1"}},
                    }
                )
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "capability_unavailable"
    assert saves == []


def test_raw_yaml_editor_counts_dropping_a_blocked_field_as_a_change(branded, monkeypatch):
    """整份替换的 YAML 编辑器里，删掉一个受管制字段就是改动，必须拦。"""
    disk = {"display": {"language": "zh"}, "providers": {"llano-gateway": {}}}
    monkeypatch.setattr(ws, "read_raw_config", lambda: dict(disk))
    monkeypatch.setattr(ws, "save_config", lambda config, merge_existing=True: pytest.fail("must not save"))

    with pytest.raises(ws.HTTPException) as exc_info:
        asyncio.run(ws.update_config_raw(ws.RawConfigUpdate(yaml_text="display:\n  language: zh\n")))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "capability_unavailable"


def test_config_read_is_sanitized_for_branded_product(branded, monkeypatch):
    monkeypatch.setattr(
        ws,
        "load_config",
        lambda: {
            "display": {"theme": "light"},
            "model": {"provider": "openrouter", "default": "anthropic/claude"},
            "providers": {"openrouter": {"base_url": "https://example.invalid"}},
        },
    )

    result = asyncio.run(ws.get_config())

    assert result["display"] == {"theme": "light"}
    assert result["model"] == "anthropic/claude"
    assert result["providers"] == {}


def test_custom_endpoints_reject_before_config_read(branded, monkeypatch):
    monkeypatch.setattr(ws, "load_config", lambda: pytest.fail("config read must not run"))

    with pytest.raises(ws.HTTPException) as exc_info:
        ws.list_custom_endpoints()

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "capability_unavailable"


def test_whatsapp_onboarding_rejects_before_session_read(branded, monkeypatch):
    monkeypatch.setattr(ws, "_whatsapp_session_path", lambda: pytest.fail("session read must not run"))

    with pytest.raises(ws.HTTPException) as exc_info:
        asyncio.run(ws.start_whatsapp_onboarding(ws.WhatsAppOnboardingStart(mode="bot")))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "capability_unavailable"


def test_env_write_rejects_provider_key_but_keeps_tool_key(branded, monkeypatch):
    from hermes_cli import credential_lifecycle

    saves = []
    monkeypatch.setattr(
        ws,
        "_catalog_provider_env_metadata",
        lambda: {"OPENROUTER_API_KEY": {"provider": "openrouter"}},
    )
    monkeypatch.setattr(
        credential_lifecycle,
        "save_provider_env_credential",
        lambda key, value: saves.append((key, value)) or {"ok": True},
    )

    with pytest.raises(ws.HTTPException) as exc_info:
        asyncio.run(
            ws.set_env_var(
                ws.EnvVarUpdate(key="OPENROUTER_API_KEY", value="secret")
            )
        )
    assert exc_info.value.detail == "capability_unavailable"
    assert saves == []

    result = asyncio.run(
        ws.set_env_var(ws.EnvVarUpdate(key="BRAVE_API_KEY", value="tool-secret"))
    )
    assert result == {"ok": True}
    assert saves == [("BRAVE_API_KEY", "tool-secret")]


def test_raw_config_rejects_blocked_structure_before_save(branded, monkeypatch):
    saves = []
    monkeypatch.setattr(ws, "save_config", lambda *args, **kwargs: saves.append(args))

    with pytest.raises(ws.HTTPException) as exc_info:
        asyncio.run(
            ws.update_config_raw(
                ws.RawConfigUpdate(yaml_text="providers:\n  openrouter: {}\n")
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "capability_unavailable"
    assert saves == []
