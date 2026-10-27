"""Regression tests for profile-scoped dashboard Channels endpoints.

Before the ``profile`` parameter existed, ``/api/messaging/platforms`` always
read/wrote the dashboard process's own (root) ``.env`` via ``load_env()`` /
``save_env_value()`` — so a dashboard switched to a freshly created profile
still displayed and persisted the ROOT install's messaging credentials.
These tests pin the new behavior: reads and writes land in the REQUESTED
profile's HERMES_HOME, and the dashboard's own profile stays untouched.
"""
import pytest
import yaml


_VALID_WORKER_BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234"
_VALID_BODY_BOT_TOKEN = "987654321:ZYXWVUTSRQPONMLKJIHGFEDCBA_4321"


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch, _isolate_hermes_home):
    """Isolated default home + one named profile, each with its own .env."""
    from hermes_constants import get_hermes_home
    from hermes_cli import profiles

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"
    for home in (default_home, worker_home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    (default_home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=root-token\n", encoding="utf-8"
    )
    (worker_home / ".env").write_text("", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home}


@pytest.fixture
def client(monkeypatch, isolated_profiles):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    # The dashboard process's os.environ may carry root-install credentials;
    # make sure the scoped path never falls back to them.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _telegram(payload):
    return next(p for p in payload["platforms"] if p["id"] == "telegram")


def _env_field(platform, key):
    return next(f for f in platform["env_vars"] if f["key"] == key)


class TestProfileScopedMessagingReads:
    def test_scoped_read_does_not_show_root_credentials(
        self, client, isolated_profiles
    ):
        resp = client.get(
            "/api/messaging/platforms", params={"profile": "worker_alpha"}
        )
        assert resp.status_code == 200
        telegram = _telegram(resp.json())
        token = _env_field(telegram, "TELEGRAM_BOT_TOKEN")
        # The worker profile has an empty .env — the root token must not leak.
        assert token["is_set"] is False
        assert telegram["configured"] is False


    def test_unknown_profile_returns_404(self, client, isolated_profiles):
        resp = client.get(
            "/api/messaging/platforms", params={"profile": "no_such_profile"}
        )
        assert resp.status_code == 404

    def test_scoped_read_returns_profile_path_command_and_startup_failure(
        self, client, isolated_profiles, monkeypatch
    ):
        import hermes_cli.web_server as web_server

        worker_home = isolated_profiles["worker_alpha"]
        (worker_home / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=worker-token\n", encoding="utf-8"
        )
        (worker_home / "config.yaml").write_text(
            yaml.safe_dump({"platforms": {"telegram": {"enabled": True}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(web_server, "get_running_pid", lambda *a, **k: None)
        monkeypatch.setattr(
            web_server, "get_running_pid_cached", lambda *a, **k: None
        )
        monkeypatch.setattr(
            web_server,
            "read_runtime_status",
            # Accepts path= : the profile-scoped read now passes the
            # profile's own gateway_state.json explicitly rather than
            # relying on process-level HERMES_HOME resolution (#71211).
            lambda *a, **k: {
                "gateway_state": "startup_failed",
                "exit_reason": "all configured messaging platforms failed to connect",
                "platforms": {},
            },
        )

        resp = client.get(
            "/api/messaging/platforms", params={"profile": "worker_alpha"}
        )

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["env_path"] == str(worker_home / ".env")
        assert payload["gateway_start_command"] == (
            "hermes -p worker_alpha gateway start"
        )
        telegram = _telegram(payload)
        assert telegram["state"] == "startup_failed"
        assert telegram["error_code"] == "startup_failed"
        assert telegram["error_message"] == (
            "all configured messaging platforms failed to connect"
        )


class TestProfileScopedMessagingWrites:
    def test_scoped_write_lands_in_target_profile_env(
        self, client, isolated_profiles
    ):
        resp = client.put(
            "/api/messaging/platforms/telegram",
            params={"profile": "worker_alpha"},
            json={
                "enabled": True,
                "env": {"TELEGRAM_BOT_TOKEN": _VALID_WORKER_BOT_TOKEN},
            },
        )
        assert resp.status_code == 200

        worker_env = (
            isolated_profiles["worker_alpha"] / ".env"
        ).read_text(encoding="utf-8")
        assert f"TELEGRAM_BOT_TOKEN={_VALID_WORKER_BOT_TOKEN}" in worker_env

        # The dashboard's own .env must stay untouched — this was the bug.
        root_env = (isolated_profiles["default"] / ".env").read_text(
            encoding="utf-8"
        )
        assert _VALID_WORKER_BOT_TOKEN not in root_env
        assert "TELEGRAM_BOT_TOKEN=root-token" in root_env

        # Enablement lands in the target profile's config.yaml.
        worker_cfg = yaml.safe_load(
            (isolated_profiles["worker_alpha"] / "config.yaml").read_text()
        ) or {}
        assert worker_cfg.get("platforms", {}).get("telegram", {}).get("enabled") is True
        root_cfg = yaml.safe_load(
            (isolated_profiles["default"] / "config.yaml").read_text()
        ) or {}
        assert "telegram" not in (root_cfg.get("platforms") or {})


    def test_scoped_read_after_scoped_write_round_trips(
        self, client, isolated_profiles
    ):
        client.put(
            "/api/messaging/platforms/telegram",
            params={"profile": "worker_alpha"},
            json={
                "enabled": True,
                "env": {"TELEGRAM_BOT_TOKEN": _VALID_WORKER_BOT_TOKEN},
            },
        )
        resp = client.get(
            "/api/messaging/platforms", params={"profile": "worker_alpha"}
        )
        telegram = _telegram(resp.json())
        assert telegram["enabled"] is True
        assert _env_field(telegram, "TELEGRAM_BOT_TOKEN")["is_set"] is True
        assert telegram["configured"] is True



def _enable_multiplex(default_home):
    (default_home / "config.yaml").write_text(
        yaml.safe_dump({"gateway": {"multiplex_profiles": True}}),
        encoding="utf-8",
    )


class TestMultiplexPortBindingGuard:
    """Enabling a port-binding channel on a secondary multiplexed profile
    must be rejected BEFORE anything is persisted.

    The gateway fail-fasts with ``MultiplexConfigError`` when a secondary
    profile enables a port-binding platform under
    ``gateway.multiplex_profiles`` — but the dashboard used to persist that
    exact config, so the next gateway start died for EVERY profile (#62791).
    """

    @pytest.fixture(autouse=True)
    def _no_multiplex_env_override(self, monkeypatch):
        # The operator env override must not leak into these tests: the
        # multiplex flag under test comes from the default profile's config.
        monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)

    def test_rejects_every_port_binding_platform_on_secondary(
        self, client, isolated_profiles
    ):
        from gateway.config import PORT_BINDING_PLATFORM_VALUES

        _enable_multiplex(isolated_profiles["default"])
        assert PORT_BINDING_PLATFORM_VALUES  # guard set must not be empty
        for platform_id in sorted(PORT_BINDING_PLATFORM_VALUES):
            resp = client.put(
                f"/api/messaging/platforms/{platform_id}",
                params={"profile": "worker_alpha"},
                json={"enabled": True},
            )
            assert resp.status_code == 409, platform_id
            assert "default profile" in resp.json()["detail"]





    def test_secondary_can_disable_and_clear_invalid_config(
        self, client, isolated_profiles
    ):
        _enable_multiplex(isolated_profiles["default"])
        worker_home = isolated_profiles["worker_alpha"]
        (worker_home / "config.yaml").write_text(
            yaml.safe_dump({"platforms": {"api_server": {"enabled": True}}}),
            encoding="utf-8",
        )

        resp = client.put(
            "/api/messaging/platforms/api_server",
            params={"profile": "worker_alpha"},
            json={"enabled": False},
        )
        assert resp.status_code == 200
        cfg = yaml.safe_load((worker_home / "config.yaml").read_text())
        assert cfg["platforms"]["api_server"]["enabled"] is False

        catalog = client.get(
            "/api/messaging/platforms", params={"profile": "worker_alpha"}
        ).json()
        api_server = next(p for p in catalog["platforms"] if p["id"] == "api_server")
        if api_server["env_vars"]:
            resp = client.put(
                "/api/messaging/platforms/api_server",
                params={"profile": "worker_alpha"},
                json={"clear_env": [api_server["env_vars"][0]["key"]]},
            )
            assert resp.status_code == 200



class TestBrandedUnrestrictedMessaging:
    @pytest.fixture
    def unrestricted_managed(self, tmp_path, monkeypatch):
        """Use the real managed loader, with all other brand gates intact."""
        from hermes_cli.managed_scope import invalidate_managed_cache

        managed = tmp_path / "managed-open-connectors"
        managed.mkdir()
        (managed / "config.yaml").write_text(yaml.safe_dump({
            "product": {
                "enabled": True,
                "brand_id": "llano-ai-workbench",
                "provider_id": "llano-gateway",
                "allowed_messaging_platforms": [],
                "disabled_capabilities": ["provider_management", "nous_services"],
            },
        }), encoding="utf-8")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        invalidate_managed_cache()
        yield managed
        invalidate_managed_cache()

    def test_branded_catalog_restores_registered_connectors(
        self, client, unrestricted_managed, monkeypatch
    ):
        from hermes_cli import web_server as ws, product_policy

        payload = client.get("/api/messaging/platforms", params={"profile": "worker_alpha"})
        assert payload.status_code == 200
        shown = {p["id"] for p in payload.json()["platforms"]}
        assert {"feishu", "wecom", "slack", "discord"} <= shown
        # Compare against the same registered catalog in stock mode, rather
        # than freezing a platform count or omitting future plugins.
        monkeypatch.setattr(product_policy, "messaging_platform_allowed", lambda _: True)
        registered = {entry["id"] for entry in ws._messaging_platform_catalog()}
        assert shown == registered
        assert not product_policy.capability_allowed("provider_management")
        assert not product_policy.capability_allowed("nous_services")

    @pytest.mark.parametrize("platform", ["feishu", "wecom", "slack", "discord"])
    def test_rest_config_roundtrip_without_connecting_external_platform(
        self, client, unrestricted_managed, isolated_profiles, platform
    ):
        home = isolated_profiles["worker_alpha"]
        (home / "config.yaml").write_text(yaml.safe_dump({"display": {"language": "zh"}}))
        for enabled in (True, False):
            response = client.put(
                f"/api/messaging/platforms/{platform}",
                params={"profile": "worker_alpha"},
                json={"enabled": enabled, "env": {}},
            )
            assert response.status_code == 200, response.text
            saved = yaml.safe_load((home / "config.yaml").read_text())
            assert saved["platforms"][platform]["enabled"] is enabled
            assert saved["display"]["language"] == "zh"
        # No credentials, gateway start, test route, or outbound message.
        assert (home / ".env").read_text() == ""
        assert platform not in (yaml.safe_load(
            (isolated_profiles["default"] / "config.yaml").read_text()
        ).get("platforms") or {})
