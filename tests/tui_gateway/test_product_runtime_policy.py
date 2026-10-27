import pytest

from hermes_cli import product_policy
from tui_gateway import server


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


@pytest.mark.parametrize(
    "row",
    [
        {"model": "claude", "billing_provider": "anthropic"},
        {"model": "acme/model", "model_config": {"provider": "custom:acme", "base_url": "https://acme.invalid/v1"}},
        {"model": "legacy", "billing_provider": "custom", "model_config": {"api_mode": "chat_completions"}},
        {"model": "hermes-3", "billing_provider": "nous"},
        {
            "model": "aggregate",
            "model_config": {
                "provider": "openrouter",
                "reasoning_config": {"effort": "high"},
                "service_tier": "priority",
                "moa": {"enabled": True},
            },
        },
    ],
)
def test_branded_historical_runtime_forces_managed_gateway(monkeypatch, row):
    _patch_managed(monkeypatch, product_policy, lambda: {
            "product": {
                "enabled": True,
                "brand_id": "llano-ai-workbench",
                "provider_id": "llano-gateway",
                "disabled_capabilities": ["provider_management", "moa"],
            }
        })

    overrides = server._stored_session_runtime_overrides(row)

    assert overrides == {
        "model_override": {
            "model": row["model"],
            "provider": "llano-gateway",
            "base_url": None,
            "api_mode": None,
        },
        "provider_override": "llano-gateway",
    }
