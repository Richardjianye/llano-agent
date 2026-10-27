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


def _branded_policy():
    return {
        "product": {
            "enabled": True,
            "brand_id": "llano-ai-workbench",
            "provider_id": "llano-gateway",
            "disabled_capabilities": [
                "nous_services",
                "moa",
                "pet_ai_generation",
            ],
        }
    }


def test_branded_catalog_omits_blocked_commands(monkeypatch):
    _patch_managed(monkeypatch, product_policy, _branded_policy)

    response = server._methods["commands.catalog"]("r1", {})
    names = {pair[0] for pair in response["result"]["pairs"]}

    assert "/pet" in names
    assert names.isdisjoint({"/moa", "/subscription", "/topup", "/hatch"})

    completions = server._methods["complete.slash"]("r2", {"text": "/"})
    completion_names = {item["text"] for item in completions["result"]["items"]}
    assert completion_names.isdisjoint({"/moa", "/subscription", "/topup", "/hatch"})


def test_branded_direct_slash_rejects_before_worker_creation(monkeypatch):
    _patch_managed(monkeypatch, product_policy, _branded_policy)
    monkeypatch.setitem(server._sessions, "blocked", {"agent": None})
    monkeypatch.setattr(server, "_SlashWorker", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker")))

    response = server._methods["slash.exec"](
        "r1", {"session_id": "blocked", "command": "/upgrade"}
    )

    assert response["error"]["data"] == {
        "reason": "capability_unavailable",
        "capability": "nous_services",
    }


def test_branded_pet_ai_and_billing_rpc_return_stable_policy_error(monkeypatch):
    _patch_managed(monkeypatch, product_policy, _branded_policy)

    for method in ("pet.generate", "pet.hatch", "billing.state", "subscription.state"):
        response = server._methods[method]("r1", {})
        assert response["error"]["data"] == {
            "reason": "capability_unavailable",
            "capability": "pet_ai_generation" if method.startswith("pet.") else "nous_services",
        }

    ordinary_pet = server._methods["pet.gallery"]("r2", {})
    assert "result" in ordinary_pet
