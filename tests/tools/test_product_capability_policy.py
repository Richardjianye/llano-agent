from hermes_cli import product_policy


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


def _branded_policy():
    return {
        "product": {
            "enabled": True,
            "brand_id": "llano-ai-workbench",
            "provider_id": "llano-gateway",
            "disabled_capabilities": [
                "builtin_image_generation",
                "nous_services",
            ],
        }
    }


def test_builtin_image_generation_rejects_before_credentials(monkeypatch):
    import tools.image_generation_tool as image_tool

    _patch_managed(monkeypatch, product_policy, _branded_policy)
    monkeypatch.setattr(
        image_tool,
        "check_fal_api_key",
        lambda: (_ for _ in ()).throw(AssertionError("credential read")),
    )

    assert image_tool.check_image_generation_requirements() is False


def test_nous_tool_gateway_rejects_before_account_or_token_reads(monkeypatch):
    from hermes_cli import nous_account
    from tools.managed_tool_gateway import resolve_managed_tool_gateway

    _patch_managed(monkeypatch, product_policy, _branded_policy)
    account_reads = []
    monkeypatch.setattr(
        nous_account,
        "get_nous_portal_account_info",
        lambda *args, **kwargs: account_reads.append(True),
    )

    assert resolve_managed_tool_gateway(
        "fal",
        token_reader=lambda: (_ for _ in ()).throw(AssertionError("token read")),
    ) is None
    assert account_reads == []
