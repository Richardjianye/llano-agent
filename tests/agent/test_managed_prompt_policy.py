"""Managed company-prompt policy: validation, LKG fallback, and session pinning."""

from __future__ import annotations

import json
import threading
from io import BytesIO
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest


def _policy_payload(core="CORE", persona="PERSONA", **overrides):
    from agent.managed_prompt_policy import prompt_policy_content_hash

    core = overrides.pop("company_core_policy", core)
    persona = overrides.pop("default_persona", persona)
    payload = {
        "version": "policy-v2",
        "company_core_policy": core,
        "default_persona": persona,
        "published_at": "2026-09-17T00:00:00Z",
        "content_hash": prompt_policy_content_hash(core, persona),
    }
    payload.update(overrides)
    return payload


def _product_policy():
    return SimpleNamespace(
        brand_id="llano-ai-workbench",
        environment="production",
        gateway_origin="https://gateway.example.test/",
    )


def _cache_envelope(payload, *, environment="production"):
    return {
        **payload,
        "object": "llano.prompt_policy",
        "scope": {
            "brand_id": "llano-ai-workbench",
            "environment": environment,
            "gateway_origin": "https://gateway.example.test",
        },
    }


def test_content_hash_normalizes_all_line_endings():
    from agent.managed_prompt_policy import prompt_policy_content_hash

    expected = prompt_policy_content_hash("a\nb\nc", "p\nq")
    assert prompt_policy_content_hash(" a\r\nb\rc \n", " p\rq ") == expected


def test_content_hash_matches_fixed_gateway_contract_vector():
    from agent.managed_prompt_policy import prompt_policy_content_hash

    # Shared verbatim with audit-gateway/server/tests/test_prompt_policy.py.
    assert prompt_policy_content_hash(" 规则一\r\n规则二 ", " 人格一\r人格二 ") == (
        "sha256:c64a3f850359c37ddd08125e5b6f753efbba371bc3486e1139f566d098595e09"
    )


def test_public_gateway_integer_version_is_normalized_to_string():
    from agent.managed_prompt_policy import snapshot_from_payload

    snapshot = snapshot_from_payload(_policy_payload(version=1), source="gateway")

    assert snapshot.version == "1"


def test_remote_success_is_validated_and_atomically_cached(monkeypatch, tmp_path):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_CACHE_FILENAME,
        resolve_latest_product_prompt_policy,
    )

    payload = _policy_payload()
    writes = []

    def fake_atomic_write(path, content, **kwargs):
        writes.append((path, content, kwargs))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr("agent.managed_prompt_policy.atomic_write_text", fake_atomic_write)
    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda url, headers: payload,
    )

    assert snapshot.version == "policy-v2"
    assert snapshot.company_core_policy == "CORE"
    assert snapshot.default_persona == "PERSONA"
    assert snapshot.source == "gateway"
    assert writes and writes[0][0] == tmp_path / "cache" / PROMPT_POLICY_CACHE_FILENAME
    cached = json.loads(writes[0][1])
    assert cached["content_hash"] == payload["content_hash"]
    assert cached["scope"] == _cache_envelope({})["scope"]
    assert writes[0][2]["create_mode"] == 0o600


@pytest.mark.parametrize(
    "remote_payload",
    [
        pytest.param(_policy_payload(content_hash="sha256:" + "0" * 64), id="bad-hash"),
        pytest.param(
            _policy_payload(company_core_policy="x" * 40_000),
            id="oversized",
        ),
        pytest.param(
            _policy_payload(
                expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z")
            ),
            id="expired-remote",
        ),
    ],
)
def test_invalid_remote_falls_back_to_unexpired_lkg(
    monkeypatch, tmp_path, remote_payload
):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_CACHE_FILENAME,
        resolve_latest_product_prompt_policy,
    )

    lkg = _policy_payload(core="LKG CORE", persona="LKG PERSONA")
    cache_path = tmp_path / "cache" / PROMPT_POLICY_CACHE_FILENAME
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(_cache_envelope(lkg), ensure_ascii=False), encoding="utf-8"
    )

    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda _url, _headers: remote_payload,
    )

    assert snapshot.company_core_policy == "LKG CORE"
    assert snapshot.default_persona == "LKG PERSONA"
    assert snapshot.source == "lkg"


def test_lkg_is_never_reused_across_product_environments(tmp_path):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_CACHE_FILENAME,
        resolve_latest_product_prompt_policy,
    )
    from hermes_cli.default_prompt_policy import BUNDLED_COMPANY_CORE_POLICY

    test_lkg = _policy_payload(core="TEST CORE", persona="TEST PERSONA")
    cache_path = tmp_path / "cache" / PROMPT_POLICY_CACHE_FILENAME
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(_cache_envelope(test_lkg, environment="test"), ensure_ascii=False),
        encoding="utf-8",
    )

    def offline(_url, _headers):
        raise OSError("offline")

    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=offline,
    )

    assert snapshot.source == "bundled"
    assert snapshot.company_core_policy == BUNDLED_COMPANY_CORE_POLICY
    assert snapshot.company_core_policy != "TEST CORE"


def test_legacy_unscoped_lkg_is_ignored(tmp_path):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_CACHE_FILENAME,
        resolve_latest_product_prompt_policy,
    )

    cache_path = tmp_path / "cache" / PROMPT_POLICY_CACHE_FILENAME
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(_policy_payload()), encoding="utf-8")

    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    )

    assert snapshot.source == "bundled"


def test_network_failure_without_lkg_uses_bundled_baseline(tmp_path):
    from agent.managed_prompt_policy import resolve_latest_product_prompt_policy
    from hermes_cli.default_prompt_policy import BUNDLED_COMPANY_CORE_POLICY
    from hermes_cli.default_soul import BRANDED_DEFAULT_SOUL_MD

    def offline(_url, _headers):
        raise OSError("offline")

    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=offline,
    )

    assert snapshot.company_core_policy == BUNDLED_COMPANY_CORE_POLICY
    assert snapshot.default_persona == BRANDED_DEFAULT_SOUL_MD
    assert snapshot.source == "bundled"


def test_stock_hermes_does_not_fetch_or_inject_policy(tmp_path):
    from agent.managed_prompt_policy import resolve_latest_product_prompt_policy

    fetch = MagicMock()
    assert (
        resolve_latest_product_prompt_policy(
            api_key="key",
            product_policy=None,
            hermes_home=tmp_path,
            fetch_json=fetch,
        )
        is None
    )
    fetch.assert_not_called()


def test_existing_session_snapshot_wins_without_network(monkeypatch):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_MODEL_CONFIG_KEY,
        initialize_agent_prompt_policy,
        snapshot_from_payload,
    )

    stored = snapshot_from_payload(_policy_payload(), source="gateway")
    db = MagicMock()
    db.get_session.return_value = {
        "system_prompt": "ORIGINAL BYTES",
        "model_config": json.dumps(
            {PROMPT_POLICY_MODEL_CONFIG_KEY: stored.to_model_config()}
        ),
    }
    agent = SimpleNamespace(
        _session_db=db,
        session_id="existing",
        _parent_session_id=None,
        api_key="key",
        _session_init_model_config={},
    )
    monkeypatch.setattr(
        "agent.managed_prompt_policy.resolve_latest_product_prompt_policy",
        MagicMock(side_effect=AssertionError("resume must not fetch")),
    )

    result = initialize_agent_prompt_policy(agent, product_policy=_product_policy())

    assert result.version == stored.version
    assert agent._prompt_policy_snapshot.company_core_policy == "CORE"
    assert (
        agent._session_init_model_config[PROMPT_POLICY_MODEL_CONFIG_KEY]["version"]
        == stored.version
    )


def test_live_fork_inherits_snapshot_without_db_or_network(monkeypatch):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_MODEL_CONFIG_KEY,
        initialize_agent_prompt_policy,
        snapshot_from_payload,
    )

    parent_snapshot = snapshot_from_payload(_policy_payload(version=3), source="gateway")
    agent = SimpleNamespace(
        _inherited_prompt_policy_snapshot=parent_snapshot,
        _session_db=None,
        session_id="ephemeral-fork",
        _parent_session_id="parent",
        api_key="key",
        _session_init_model_config={},
    )
    resolver = MagicMock(side_effect=AssertionError("fork must not fetch"))
    monkeypatch.setattr(
        "agent.managed_prompt_policy.resolve_latest_product_prompt_policy", resolver
    )

    result = initialize_agent_prompt_policy(agent, product_policy=_product_policy())

    assert result is parent_snapshot
    assert (
        agent._session_init_model_config[PROMPT_POLICY_MODEL_CONFIG_KEY]["version"]
        == "3"
    )
    resolver.assert_not_called()


def test_legacy_session_with_stored_prompt_is_pinned_without_new_policy(monkeypatch):
    from agent.managed_prompt_policy import initialize_agent_prompt_policy

    db = MagicMock()
    db.get_session.return_value = {
        "system_prompt": "PRE-POLICY SYSTEM PROMPT",
        "model_config": None,
    }
    agent = SimpleNamespace(
        _session_db=db,
        session_id="legacy",
        _parent_session_id=None,
        api_key="key",
        _session_init_model_config={},
    )
    resolver = MagicMock(side_effect=AssertionError("legacy resume must not fetch"))
    monkeypatch.setattr(
        "agent.managed_prompt_policy.resolve_latest_product_prompt_policy", resolver
    )

    result = initialize_agent_prompt_policy(agent, product_policy=_product_policy())

    assert result.version == "legacy:no-managed-policy"
    assert result.company_core_policy == ""
    resolver.assert_not_called()


def test_fresh_session_freezes_and_persists_latest_snapshot(monkeypatch):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_MODEL_CONFIG_KEY,
        initialize_agent_prompt_policy,
        snapshot_from_payload,
    )

    latest = snapshot_from_payload(_policy_payload(), source="gateway")
    db = MagicMock()
    db.get_session.return_value = {
        "system_prompt": None,
        "model_config": None,
    }
    agent = SimpleNamespace(
        _session_db=db,
        session_id="fresh",
        _parent_session_id=None,
        api_key="key",
        _session_init_model_config={},
    )
    monkeypatch.setattr(
        "agent.managed_prompt_policy.resolve_latest_product_prompt_policy",
        MagicMock(return_value=latest),
    )

    result = initialize_agent_prompt_policy(agent, product_policy=_product_policy())

    assert result is latest
    saved = agent._session_init_model_config[PROMPT_POLICY_MODEL_CONFIG_KEY]
    assert saved["company_core_policy"] == "CORE"
    db.patch_session_model_config.assert_called_once_with(
        "fresh", {PROMPT_POLICY_MODEL_CONFIG_KEY: saved}
    )


def test_parent_lineage_alone_does_not_inherit_policy(monkeypatch):
    """Gateway /new and /reset use parent ids only for audit provenance."""
    from agent.managed_prompt_policy import (
        initialize_agent_prompt_policy,
        snapshot_from_payload,
    )

    old = snapshot_from_payload(_policy_payload(core="OLD"), source="gateway")
    latest = snapshot_from_payload(_policy_payload(core="LATEST"), source="gateway")
    db = MagicMock()
    db.get_session.side_effect = lambda session_id: {
        "new": {
            "parent_session_id": "old",
            "system_prompt": None,
            "model_config": None,
        },
        "old": {
            "system_prompt": "OLD PROMPT",
            "model_config": json.dumps(
                {"prompt_policy_snapshot": old.to_model_config()}
            ),
        },
    }.get(session_id)
    agent = SimpleNamespace(
        _session_db=db,
        session_id="new",
        _parent_session_id=None,
        api_key="key",
        _session_init_model_config={},
    )
    resolver = MagicMock(return_value=latest)
    monkeypatch.setattr(
        "agent.managed_prompt_policy.resolve_latest_product_prompt_policy",
        resolver,
    )

    result = initialize_agent_prompt_policy(agent, product_policy=_product_policy())

    assert result.company_core_policy == "LATEST"
    resolver.assert_called_once()
    assert [call.args[0] for call in db.get_session.call_args_list] == ["new"]


def test_exact_unpublished_404_skips_existing_lkg(monkeypatch, tmp_path):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_CACHE_FILENAME,
        _fetch_json,
        resolve_latest_product_prompt_policy,
    )

    cache_path = tmp_path / "cache" / PROMPT_POLICY_CACHE_FILENAME
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            _cache_envelope(
                _policy_payload(core="STALE LKG", persona="STALE PERSONA")
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    body = json.dumps(
        {
            "code": 10011,
            "message": "unpublished",
            "data": {
                "reason": "prompt_policy_unpublished",
                "fallback": "bundled",
            },
        }
    ).encode()
    error = HTTPError(
        "https://gateway.example.test/v1/prompt-policy/effective",
        404,
        "Not Found",
        {},
        BytesIO(body),
    )
    monkeypatch.setattr(
        "agent.managed_prompt_policy.urlopen",
        MagicMock(side_effect=error),
    )

    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=_fetch_json,
    )

    assert snapshot.source == "bundled"
    assert snapshot.company_core_policy != "STALE LKG"


def test_non_contract_404_keeps_lkg(monkeypatch, tmp_path):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_CACHE_FILENAME,
        _fetch_json,
        resolve_latest_product_prompt_policy,
    )

    cache_path = tmp_path / "cache" / PROMPT_POLICY_CACHE_FILENAME
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(_cache_envelope(_policy_payload(core="LKG"))),
        encoding="utf-8",
    )
    error = HTTPError(
        "https://gateway.example.test/v1/prompt-policy/effective",
        404,
        "Not Found",
        {},
        BytesIO(b'{"code":404,"data":{"reason":"other"}}'),
    )
    monkeypatch.setattr(
        "agent.managed_prompt_policy.urlopen",
        MagicMock(side_effect=error),
    )

    snapshot = resolve_latest_product_prompt_policy(
        api_key="device-key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=_fetch_json,
    )

    assert snapshot.source == "lkg"
    assert snapshot.company_core_policy == "LKG"


def test_transport_cap_allows_server_valid_high_escape_payload():
    from agent.managed_prompt_policy import _read_bounded_json, snapshot_from_payload

    core = "界" * 20_000
    persona = "人" * 8_000
    payload = _policy_payload(core=core, persona=persona)
    encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    assert len(encoded) > 128_000

    parsed = _read_bounded_json(BytesIO(encoded))
    snapshot = snapshot_from_payload(parsed, source="gateway")

    assert snapshot.company_core_policy == core
    assert snapshot.default_persona == persona


def test_transport_read_is_bounded_before_json_parse():
    from agent.managed_prompt_policy import (
        MAX_POLICY_RESPONSE_BYTES,
        _read_bounded_json,
    )

    class OversizedResponse:
        requested = None

        def read(self, size):
            self.requested = size
            return b"x" * size

    response = OversizedResponse()
    with pytest.raises(ValueError, match="response is too large"):
        _read_bounded_json(response)

    assert response.requested == MAX_POLICY_RESPONSE_BYTES + 1


def test_remote_version_never_rolls_back_scoped_lkg(tmp_path):
    from agent.managed_prompt_policy import resolve_latest_product_prompt_policy

    newer = _policy_payload(core="NEWER", version=12)
    older = _policy_payload(core="OLDER", version=11)
    resolve_latest_product_prompt_policy(
        api_key="key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda *_args: newer,
    )

    selected = resolve_latest_product_prompt_policy(
        api_key="key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda *_args: older,
    )

    assert selected.version == "12"
    assert selected.company_core_policy == "NEWER"
    assert selected.source == "lkg"


def test_same_version_different_hash_never_overwrites_lkg(tmp_path):
    from agent.managed_prompt_policy import resolve_latest_product_prompt_policy

    original = _policy_payload(core="ORIGINAL", version=21)
    split_brain = _policy_payload(core="SPLIT", version=21)
    resolve_latest_product_prompt_policy(
        api_key="key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda *_args: original,
    )

    selected = resolve_latest_product_prompt_policy(
        api_key="key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda *_args: split_brain,
    )

    assert selected.company_core_policy == "ORIGINAL"
    assert selected.content_hash == original["content_hash"]


def test_concurrent_out_of_order_fetch_cannot_clobber_newer_lkg(tmp_path):
    from agent.managed_prompt_policy import resolve_latest_product_prompt_policy

    slow_started = threading.Event()
    release_slow = threading.Event()
    results = {}

    def slow_fetch(*_args):
        slow_started.set()
        assert release_slow.wait(5)
        return _policy_payload(core="V31", version=31)

    def resolve(name, fetch):
        results[name] = resolve_latest_product_prompt_policy(
            api_key="key",
            product_policy=_product_policy(),
            hermes_home=tmp_path,
            fetch_json=fetch,
        )

    slow = threading.Thread(target=resolve, args=("slow", slow_fetch))
    slow.start()
    assert slow_started.wait(5)

    fast = threading.Thread(
        target=resolve,
        args=("fast", lambda *_args: _policy_payload(core="V32", version=32)),
    )
    fast.start()
    fast.join(5)
    assert not fast.is_alive()
    release_slow.set()
    slow.join(5)
    assert not slow.is_alive()

    offline = resolve_latest_product_prompt_policy(
        api_key="key",
        product_policy=_product_policy(),
        hermes_home=tmp_path,
        fetch_json=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    )
    assert results["fast"].version == "32"
    assert results["slow"].version == "32"
    assert offline.version == "32"
    assert offline.company_core_policy == "V32"


def test_invalid_product_scope_preserves_existing_frozen_snapshot(monkeypatch):
    from agent.managed_prompt_policy import (
        PROMPT_POLICY_MODEL_CONFIG_KEY,
        initialize_agent_prompt_policy,
        snapshot_from_payload,
    )
    from hermes_cli.product_policy import ProductPolicyState, ProductPolicyStatus

    stored = snapshot_from_payload(_policy_payload(version=41), source="gateway")
    db = MagicMock()
    db.get_session.return_value = {
        "system_prompt": "EXACT MANAGED BYTES",
        "model_config": json.dumps(
            {PROMPT_POLICY_MODEL_CONFIG_KEY: stored.to_model_config()}
        ),
    }
    agent = SimpleNamespace(
        _session_db=db,
        session_id="managed-invalid-resume",
        api_key="key",
        _session_init_model_config={},
    )
    monkeypatch.setattr(
        "hermes_cli.product_policy.load_product_policy_status",
        lambda: ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="temporarily unreadable",
        ),
    )
    resolver = MagicMock(side_effect=AssertionError("resume must not fetch"))
    monkeypatch.setattr(
        "agent.managed_prompt_policy.resolve_latest_product_prompt_policy",
        resolver,
    )

    selected = initialize_agent_prompt_policy(agent)

    assert selected.version == "41"
    assert agent._prompt_policy_snapshot is selected
    resolver.assert_not_called()


def test_invalid_product_scope_fresh_session_uses_bundled_safe_baseline(monkeypatch):
    from agent.managed_prompt_policy import initialize_agent_prompt_policy
    from hermes_cli.product_policy import ProductPolicyState, ProductPolicyStatus

    monkeypatch.setattr(
        "hermes_cli.product_policy.load_product_policy_status",
        lambda: ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="bad yaml",
        ),
    )
    agent = SimpleNamespace(
        _session_db=None,
        session_id="fresh-invalid",
        api_key="",
        _session_init_model_config={},
    )

    selected = initialize_agent_prompt_policy(agent)

    assert selected is not None
    assert selected.source == "bundled"
    assert selected.company_core_policy
