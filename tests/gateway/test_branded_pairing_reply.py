"""Pairing replies to unknown DM senders in branded vs stock builds.

Branded builds hide the CLI, so the reply must point the bot owner at the
desktop 消息平台 page — and the request it produces must be the one that
page's QQ pane lists and approves (``/api/pairing`` + ``/api/pairing/approve``).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.product_policy import ProductPolicyState, ProductPolicyStatus


def _clear_qq_auth_env(monkeypatch) -> None:
    for key in (
        "QQ_ALLOWED_USERS",
        "QQ_GROUP_ALLOWED_USERS",
        "QQ_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_policy(monkeypatch, state: ProductPolicyState) -> None:
    monkeypatch.setattr(
        "hermes_cli.product_policy.load_product_policy_status",
        lambda: ProductPolicyStatus(state),
    )


def _qq_dm(user_id: str = "QQ_OPENID_1") -> MessageEvent:
    return MessageEvent(
        text="你好",
        message_id="m1",
        source=SessionSource(
            platform=Platform.QQBOT,
            user_id=user_id,
            chat_id=user_id,
            user_name="小王",
            chat_type="dm",
        ),
    )


def _make_runner(pairing_store):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.QQBOT: PlatformConfig(enabled=True)},
    )
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.QQBOT: adapter}
    runner.pairing_store = pairing_store
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompts = {}
    runner.hooks = SimpleNamespace(dispatch=AsyncMock(return_value=None))
    runner._sessions = {}
    return runner, adapter


def _desktop_client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


@pytest.mark.asyncio
async def test_branded_qq_pairing_reply_points_to_desktop_and_is_approvable(
    monkeypatch, _isolate_hermes_home,
):
    from gateway.pairing import PairingStore

    _clear_qq_auth_env(monkeypatch)
    _set_policy(monkeypatch, ProductPolicyState.ACTIVE)
    store = PairingStore()
    runner, adapter = _make_runner(store)

    assert await runner._handle_message(_qq_dm()) is None

    adapter.send.assert_awaited_once()
    reply = adapter.send.await_args.args[1]
    assert "消息平台" in reply
    assert "批准" in reply
    assert "hermes" not in reply.lower()
    assert "pairing approve" not in reply

    # The request the reply describes is the one the desktop QQ pane lists
    # (rows grouped by platform id "qqbot") and its Approve button grants.
    client = _desktop_client()
    pending = client.get("/api/pairing").json()["pending"]
    qq_rows = [row for row in pending if row["platform"] == "qqbot"]
    assert len(qq_rows) == 1
    assert qq_rows[0]["user_id"] == "QQ_OPENID_1"
    assert qq_rows[0]["user_name"] == "小王"
    assert qq_rows[0]["request_id"]

    r = client.post(
        "/api/pairing/approve",
        json={"platform": "qqbot", "request_id": qq_rows[0]["request_id"]},
    )
    assert r.status_code == 200
    assert store.is_approved("qqbot", "QQ_OPENID_1")


@pytest.mark.asyncio
async def test_branded_pairing_reply_includes_the_generated_code(monkeypatch):
    from unittest.mock import MagicMock

    _clear_qq_auth_env(monkeypatch)
    _set_policy(monkeypatch, ProductPolicyState.ACTIVE)
    store = MagicMock()
    store.is_approved.return_value = False
    store._is_rate_limited.return_value = False
    store.generate_code.return_value = "ABC12DEF"
    runner, adapter = _make_runner(store)

    await runner._handle_message(_qq_dm())

    assert "ABC12DEF" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_branded_rate_limited_pairing_reply_is_chinese(monkeypatch):
    from unittest.mock import MagicMock

    _clear_qq_auth_env(monkeypatch)
    _set_policy(monkeypatch, ProductPolicyState.ACTIVE)
    store = MagicMock()
    store.is_approved.return_value = False
    store._is_rate_limited.return_value = False
    store.generate_code.return_value = None
    runner, adapter = _make_runner(store)

    await runner._handle_message(_qq_dm())

    reply = adapter.send.await_args.args[1]
    assert "稍后" in reply
    assert "Please try again" not in reply


@pytest.mark.asyncio
async def test_stock_pairing_reply_keeps_cli_instruction(monkeypatch):
    from unittest.mock import MagicMock

    _clear_qq_auth_env(monkeypatch)
    _set_policy(monkeypatch, ProductPolicyState.STOCK)
    store = MagicMock()
    store.is_approved.return_value = False
    store._is_rate_limited.return_value = False
    store.generate_code.return_value = "ABC12DEF"
    runner, adapter = _make_runner(store)

    await runner._handle_message(_qq_dm())

    reply = adapter.send.await_args.args[1]
    assert "I don't recognize you yet" in reply
    assert "hermes pairing approve qqbot ABC12DEF" in reply
    assert "消息平台" not in reply


@pytest.mark.asyncio
async def test_unreadable_policy_falls_back_to_stock_wording(monkeypatch):
    from unittest.mock import MagicMock

    def _boom():
        raise RuntimeError("policy unreadable")

    _clear_qq_auth_env(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.product_policy.load_product_policy_status", _boom,
    )
    store = MagicMock()
    store.is_approved.return_value = False
    store._is_rate_limited.return_value = False
    store.generate_code.return_value = "ABC12DEF"
    runner, adapter = _make_runner(store)

    await runner._handle_message(_qq_dm())

    assert "hermes pairing approve qqbot ABC12DEF" in adapter.send.await_args.args[1]
