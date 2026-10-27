"""Unit tests for hermes_cli.managed_scope (resolver + loaders + key helpers)."""
import textwrap

import pytest


# ── Directory resolver ───────────────────────────────────────────────────────






# ── Loaders + key helpers ────────────────────────────────────────────────────


def _write_managed(tmp_path, monkeypatch, *, config=None, env=None):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    if config is not None:
        (managed / "config.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    if env is not None:
        (managed / ".env").write_text(textwrap.dedent(env), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    return managed








def test_load_managed_env_and_is_env_managed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _write_managed(
        tmp_path, monkeypatch, env="OPENAI_API_BASE=https://org.example/v1\n"
    )
    assert managed_scope.load_managed_env() == {
        "OPENAI_API_BASE": "https://org.example/v1"
    }
    assert managed_scope.is_env_managed("OPENAI_API_BASE") is True
    assert managed_scope.is_env_managed("OTHER") is False




def test_managed_dir_env_scrubbed_by_default():
    """conftest must scrub HERMES_MANAGED_DIR so a dev-shell value can't leak in."""
    import os

    assert "HERMES_MANAGED_DIR" not in os.environ


def test_managed_config_status_distinguishes_absent_from_declared_missing(
    tmp_path, monkeypatch
):
    from hermes_cli import managed_scope

    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    managed_scope.invalidate_managed_cache()
    assert (
        managed_scope.load_managed_config_status().state
        is managed_scope.ManagedConfigState.ABSENT
    )

    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "missing"))
    status = managed_scope.load_managed_config_status()
    assert status.state is managed_scope.ManagedConfigState.INVALID
    assert "missing or inaccessible" in status.reason


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (None, "config.yaml is missing"),
        ("product: [", "malformed"),
        ("- not\n- a\n- mapping\n", "not a mapping"),
    ],
)
def test_declared_managed_config_failures_are_invalid(
    tmp_path, monkeypatch, contents, reason
):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed-status"
    managed.mkdir()
    if contents is not None:
        (managed / "config.yaml").write_text(contents, encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    status = managed_scope.load_managed_config_status()

    assert status.state is managed_scope.ManagedConfigState.INVALID
    assert reason in status.reason


def test_inaccessible_declared_config_is_invalid(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed-permission"
    managed.mkdir()
    config_path = managed / "config.yaml"
    config_path.write_text("product: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    original = managed_scope._cached_read

    def unreadable(path, *args, **kwargs):
        if path == config_path:
            return None
        return original(path, *args, **kwargs)

    monkeypatch.setattr(managed_scope, "_cached_read", unreadable)
    status = managed_scope.load_managed_config_status()

    assert status.state is managed_scope.ManagedConfigState.INVALID
