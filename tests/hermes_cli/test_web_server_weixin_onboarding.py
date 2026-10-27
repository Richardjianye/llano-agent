"""Weixin (personal WeChat via Tencent iLink) QR onboarding from the dashboard.

The desktop app cannot run ``hermes gateway setup`` in branded builds, so the
messaging page drives the same iLink QR login through REST. These tests run
the real endpoints and background poller against a scripted iLink HTTP layer
(``gateway.platforms.weixin._api_get``) — no network.
"""
import asyncio
import json
import threading
import time

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    import hermes_cli.web_server as ws
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    monkeypatch.setattr(ws, "_WEIXIN_ONBOARDING_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    for key in (
        "WEIXIN_ACCOUNT_ID",
        "WEIXIN_TOKEN",
        "WEIXIN_BASE_URL",
        "WEIXIN_DM_POLICY",
        "WEIXIN_HOME_CHANNEL",
    ):
        monkeypatch.delenv(key, raising=False)
    with ws._weixin_onboarding_lock:
        ws._weixin_onboarding_sessions.clear()

    c = TestClient(app=ws.app)
    c.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    yield c
    # Stop any poller still running so it can't outlive the patched iLink.
    with ws._weixin_onboarding_lock:
        ws._weixin_onboarding_sessions.clear()


class FakeILink:
    """Scripted stand-in for iLink's GET endpoints.

    QR fetches return ``qr-1``, ``qr-2``, …; status polls pop ``statuses`` in
    order and then report ``wait`` until ``release`` is set.
    """

    def __init__(self, statuses=(), *, qr_fails=False):
        self.statuses = list(statuses)
        self.qr_fails = qr_fails
        self.qr_calls = 0
        self.status_calls = []
        self.release = threading.Event()

    async def __call__(self, session, *, base_url, endpoint, timeout_ms):
        from gateway.platforms import weixin

        if endpoint.startswith(weixin.EP_GET_BOT_QR):
            if self.qr_fails:
                raise RuntimeError("iLink GET get_bot_qrcode HTTP 503")
            self.qr_calls += 1
            return {
                "qrcode": f"qr-{self.qr_calls}",
                "qrcode_img_content": f"https://liteapp.weixin.qq.com/q/{self.qr_calls}",
            }
        assert endpoint.startswith(weixin.EP_GET_QR_STATUS)
        self.status_calls.append((base_url, endpoint))
        if self.statuses:
            return self.statuses.pop(0)
        await asyncio.sleep(0.01)
        return {"status": "wait"}


@pytest.fixture
def ilink(monkeypatch):
    from gateway.platforms import weixin

    fake = FakeILink()
    monkeypatch.setattr(weixin, "_api_get", fake)
    return fake


CONFIRMED = {
    "status": "confirmed",
    "ilink_bot_id": "bot-123@im.bot",
    "bot_token": "secret-bot-token",
    "baseurl": "https://ilink-redirect.weixin.qq.com",
    "ilink_user_id": "wx-user-1",
}


def _wait_for(client, pairing_id, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(f"/api/messaging/weixin/onboarding/{pairing_id}")
        if resp.status_code == 200 and predicate(resp.json()):
            return resp.json()
        if time.monotonic() > deadline:
            raise AssertionError(f"onboarding never reached the expected state: {resp.json()}")
        time.sleep(0.02)


def _start(client):
    resp = client.post("/api/messaging/weixin/onboarding/start")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fake_restart(monkeypatch):
    import hermes_cli.web_server as ws

    calls = []

    class Proc:
        pid = 4242

    def fake_spawn(profile=None):
        calls.append(profile)
        return Proc(), False

    monkeypatch.setattr(ws, "_spawn_gateway_restart", fake_spawn)
    return calls


def test_scan_confirm_apply_saves_cli_credentials_and_restarts(client, ilink, monkeypatch):
    from hermes_cli.config import load_config, load_env
    from hermes_constants import get_hermes_home

    restarts = _fake_restart(monkeypatch)
    ilink.statuses = [
        {"status": "wait"},
        {"status": "scaned_but_redirect", "redirect_host": "redirect.ilink.example"},
        CONFIRMED,
    ]

    started = _start(client)
    assert started["status"] == "waiting"
    assert started["qr_payload"] == "https://liteapp.weixin.qq.com/q/1"

    confirmed = _wait_for(client, started["pairing_id"], lambda p: p["status"] == "confirmed")
    assert confirmed["account_id"] == "bot-123@im.bot"
    assert "secret-bot-token" not in json.dumps(confirmed)
    # A redirect moves the remaining polls to the redirected host.
    assert ilink.status_calls[-1][0] == "https://redirect.ilink.example"

    applied = client.post(f"/api/messaging/weixin/onboarding/{started['pairing_id']}/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["ok"] is True
    assert body["platform"] == "weixin"
    assert body["restart_started"] is True
    assert body["needs_restart"] is False
    assert "secret-bot-token" not in json.dumps(body)
    assert restarts == [None]

    env = load_env()
    assert env["WEIXIN_ACCOUNT_ID"] == "bot-123@im.bot"
    assert env["WEIXIN_TOKEN"] == "secret-bot-token"
    assert env["WEIXIN_BASE_URL"] == "https://ilink-redirect.weixin.qq.com"
    assert env["WEIXIN_DM_POLICY"] == "pairing"
    assert env["WEIXIN_GROUP_POLICY"] == "disabled"
    assert env["WEIXIN_HOME_CHANNEL"] == "wx-user-1"
    assert load_config()["platforms"]["weixin"]["enabled"] is True

    account_file = get_hermes_home() / "weixin" / "accounts" / "bot-123@im.bot.json"
    saved = json.loads(account_file.read_text(encoding="utf-8"))
    assert saved["token"] == "secret-bot-token"
    assert saved["user_id"] == "wx-user-1"

    # The session is consumed: the platform list now reports Weixin configured.
    assert client.get(f"/api/messaging/weixin/onboarding/{started['pairing_id']}").status_code == 404
    platforms = client.get("/api/messaging/platforms").json()["platforms"]
    weixin_row = next(p for p in platforms if p["id"] == "weixin")
    assert weixin_row["enabled"] is True
    assert weixin_row["configured"] is True


def test_scanned_status_is_visible_before_confirmation(client, ilink):
    ilink.statuses = [{"status": "scaned"}]
    started = _start(client)
    scanned = _wait_for(client, started["pairing_id"], lambda p: p["status"] == "scanned")
    assert scanned["account_id"] is None


def test_expired_qr_is_refreshed_in_place(client, ilink):
    ilink.statuses = [{"status": "expired"}]
    started = _start(client)
    refreshed = _wait_for(
        client,
        started["pairing_id"],
        lambda p: p["qr_payload"] == "https://liteapp.weixin.qq.com/q/2",
    )
    assert refreshed["status"] == "waiting"
    # Polling continues against the new QR token.
    deadline = time.monotonic() + 5
    while not any("qrcode=qr-2" in endpoint for _, endpoint in ilink.status_calls):
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_repeatedly_expired_qr_ends_as_expired(client, ilink):
    from gateway.platforms import weixin

    ilink.statuses = [{"status": "expired"}] * (weixin.QR_MAX_REFRESHES + 1)
    started = _start(client)
    expired = _wait_for(client, started["pairing_id"], lambda p: p["status"] == "expired")
    assert expired["error"]
    assert ilink.qr_calls == weixin.QR_MAX_REFRESHES + 1


def test_incomplete_confirmation_is_an_error(client, ilink):
    ilink.statuses = [{"status": "confirmed", "ilink_bot_id": "bot-123@im.bot"}]
    started = _start(client)
    failed = _wait_for(client, started["pairing_id"], lambda p: p["status"] == "error")
    assert failed["error"]
    applied = client.post(f"/api/messaging/weixin/onboarding/{started['pairing_id']}/apply")
    assert applied.status_code == 409


def test_apply_before_confirmation_is_rejected(client, ilink):
    started = _start(client)
    applied = client.post(f"/api/messaging/weixin/onboarding/{started['pairing_id']}/apply")
    assert applied.status_code == 409


def test_cancel_stops_the_poller(client, ilink):
    started = _start(client)
    _wait_for(client, started["pairing_id"], lambda p: len(ilink.status_calls) > 0)

    resp = client.delete(f"/api/messaging/weixin/onboarding/{started['pairing_id']}")
    assert resp.status_code == 200
    assert client.get(f"/api/messaging/weixin/onboarding/{started['pairing_id']}").status_code == 404

    pollers = [t for t in threading.enumerate() if t.name == "weixin-onboarding"]
    for poller in pollers:
        poller.join(timeout=5)
    assert not any(poller.is_alive() for poller in pollers)


def test_new_setup_supersedes_an_open_one(client, ilink):
    first = _start(client)
    second = _start(client)
    assert second["qr_payload"] == "https://liteapp.weixin.qq.com/q/2"
    superseded = client.get(f"/api/messaging/weixin/onboarding/{first['pairing_id']}").json()
    assert superseded["status"] == "cancelled"


def test_qr_fetch_failure_is_reported(client, ilink):
    ilink.qr_fails = True
    resp = client.post("/api/messaging/weixin/onboarding/start")
    assert resp.status_code == 502


def test_rescan_keeps_existing_access_policy(client, ilink, monkeypatch):
    from hermes_cli.config import load_env, save_env_value

    _fake_restart(monkeypatch)
    save_env_value("WEIXIN_DM_POLICY", "allowlist")
    save_env_value("WEIXIN_ALLOWED_USERS", "wx-friend")
    save_env_value("WEIXIN_HOME_CHANNEL", "wx-home")

    ilink.statuses = [CONFIRMED]
    started = _start(client)
    _wait_for(client, started["pairing_id"], lambda p: p["status"] == "confirmed")
    assert client.post(f"/api/messaging/weixin/onboarding/{started['pairing_id']}/apply").status_code == 200

    env = load_env()
    assert env["WEIXIN_TOKEN"] == "secret-bot-token"
    assert env["WEIXIN_DM_POLICY"] == "allowlist"
    assert env["WEIXIN_ALLOWED_USERS"] == "wx-friend"
    assert env["WEIXIN_HOME_CHANNEL"] == "wx-home"


def test_desktop_backend_restarts_its_own_messaging_gateway(client, ilink, monkeypatch):
    import hermes_cli.desktop_messaging as desktop_messaging
    import hermes_cli.web_server as ws
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("HERMES_DESKTOP", "1")
    controls = []

    def fake_control(home, action, *, messaging_keys):
        controls.append((home, action, "WEIXIN_TOKEN" in messaging_keys))
        return {"ok": True, "pid": 777, "state": "starting"}

    def no_service_restart(profile=None):
        raise AssertionError("desktop backends must not restart the gateway service")

    monkeypatch.setattr(desktop_messaging, "control", fake_control)
    monkeypatch.setattr(ws, "_spawn_gateway_restart", no_service_restart)

    ilink.statuses = [CONFIRMED]
    started = _start(client)
    _wait_for(client, started["pairing_id"], lambda p: p["status"] == "confirmed")
    body = client.post(f"/api/messaging/weixin/onboarding/{started['pairing_id']}/apply").json()

    assert body["restart_started"] is True
    assert body["restart_pid"] == 777
    assert controls == [(get_hermes_home(), "restart", True)]


def test_policy_blocked_platform_cannot_start(client, ilink, monkeypatch):
    import hermes_cli.product_policy as product_policy

    monkeypatch.setattr(product_policy, "messaging_platform_allowed", lambda platform_id: platform_id != "weixin")
    resp = client.post("/api/messaging/weixin/onboarding/start")
    assert resp.status_code == 403
    assert ilink.qr_calls == 0
