"""A model name with a space is only a typo when the provider doesn't list it.

Regression for the 2026-09-11 desktop outage: the company gateway advertised a
model whose id was `豆包 Seed2.0-Lite`, and the picker answered every click with
"Model names cannot contain spaces." — for a model the gateway itself was
serving. The format check now runs only after the catalog has had its say.
"""

from unittest.mock import patch

from hermes_cli.models import validate_requested_model

GATEWAY_CATALOG = ["deepseek-v4-flash", "豆包 Seed2.0-Lite", "DeepSeek V4.1 Flash"]


def _with_catalog(models):
    probe = {
        "models": models,
        "probed_url": "https://gateway.example.com/v1/models",
        "resolved_base_url": "https://gateway.example.com/v1",
        "suggested_base_url": None,
        "used_fallback": False,
    }

    return patch("hermes_cli.models.fetch_api_models", return_value=models), patch(
        "hermes_cli.models.probe_api_models", return_value=probe
    )


def test_listed_name_with_space_is_accepted():
    fetch, probe = _with_catalog(GATEWAY_CATALOG)
    with fetch, probe:
        result = validate_requested_model(
            "豆包 Seed2.0-Lite",
            "llano-gateway",
            api_key="k",
            base_url="https://gateway.example.com/v1",
        )

    assert result["accepted"] is True
    assert result["recognized"] is True
    assert result["message"] is None


def test_unlisted_name_with_space_is_still_rejected():
    """`/model gpt 5` is still a typo — reject it, and say the name is unknown."""
    fetch, probe = _with_catalog(GATEWAY_CATALOG)
    with fetch, probe:
        result = validate_requested_model(
            "gpt 5",
            "llano-gateway",
            api_key="k",
            base_url="https://gateway.example.com/v1",
        )

    assert result["accepted"] is False
    assert result["persist"] is False
    assert "cannot contain spaces" in result["message"]


def test_unreachable_catalog_does_not_soft_accept_a_spaced_name():
    """No listing = no evidence. Don't persist a name we can't vouch for."""
    fetch, probe = _with_catalog(None)
    with fetch, probe:
        result = validate_requested_model(
            "豆包 Seed2.0-Lite",
            "llano-gateway",
            api_key="k",
            base_url="https://gateway.example.com/v1",
        )

    assert result["accepted"] is False


def test_empty_name_still_reports_the_empty_message():
    result = validate_requested_model("   ", "llano-gateway")

    assert result["accepted"] is False
    assert result["message"] == "Model name cannot be empty."
