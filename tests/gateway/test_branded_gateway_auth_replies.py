"""品牌构建里消息网关（钉钉等）对公司网关鉴权失败的回复。

设备 Key 被公司网关拒绝（401：无效／吊销／过期／账号停用）时，用户在聊天里该
看到一句能照着做的中文：回桌面端重新登录。非鉴权的运行时解析失败不能再被说成
「认证失败」。stock Hermes 的英文措辞保持不变。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.redaction_policy import RedactionPolicyUnavailable
from gateway import run as gateway_run
from gateway.config import Platform


@pytest.fixture(autouse=True)
def _clear_managed_cache():
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    yield
    managed_scope.invalidate_managed_cache()


@pytest.fixture
def branded(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("product:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))

    from hermes_cli.product_policy import ProductPolicyState, load_product_policy_status

    assert load_product_policy_status().state is not ProductPolicyState.STOCK


@pytest.fixture
def stock(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)

    from hermes_cli.product_policy import ProductPolicyState, load_product_policy_status

    assert load_product_policy_status().state is ProductPolicyState.STOCK


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


PROVIDER_401 = "⚠️ API call failed: Error code: 401 - {'error': {'message': 'API Key 已吊销'}}"


def _is_relogin_reply(text: str) -> bool:
    return "重新登录" in text and "公司网关" in text


# --- final reply / status sanitizer ------------------------------------------


@pytest.mark.parametrize("platform", [Platform.DINGTALK, Platform.TELEGRAM])
def test_branded_provider_401_reply_tells_user_to_relogin(branded, platform):
    reply = gateway_run._sanitize_gateway_final_response(platform, PROVIDER_401)

    assert _is_relogin_reply(reply)
    assert "Provider authentication failed" not in reply
    assert "已吊销" not in reply  # raw provider body stays out of chat


def test_branded_status_401_uses_same_reply(branded):
    status = gateway_run._prepare_gateway_status_message(
        Platform.DINGTALK, "lifecycle", "HTTP 401 Unauthorized"
    )

    assert _is_relogin_reply(status)


def test_stock_provider_401_reply_unchanged(stock):
    reply = gateway_run._sanitize_gateway_final_response(Platform.DINGTALK, PROVIDER_401)

    assert reply.startswith("⚠️ Provider authentication failed.")


def test_branded_non_auth_provider_errors_keep_their_category(branded):
    reply = gateway_run._sanitize_gateway_final_response(
        Platform.DINGTALK, "⚠️ API call failed: Error code: 429 - rate limit"
    )

    assert "rate-limiting" in reply
    assert not _is_relogin_reply(reply)


def test_raw_text_surfaces_still_get_raw_401(branded):
    assert (
        gateway_run._sanitize_gateway_final_response(Platform.LOCAL, PROVIDER_401)
        == PROVIDER_401
    )


# --- runtime resolution in TurnRunner.run_sync -------------------------------


def _run_sync_with_resolution_error(exc: BaseException) -> dict:
    runner = MagicMock()
    runner._get_system_prompt_for_channel.return_value = ""
    runner._resolve_session_agent_runtime.side_effect = exc
    ctx = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.DINGTALK, chat_id="c1"),
        context_prompt="",
        channel_prompt="",
        session_key="dingtalk:c1",
        user_config={},
    )
    return gateway_run.TurnRunner(runner, ctx).run_sync()


def test_branded_non_auth_resolution_error_is_not_labelled_auth(branded):
    result = _run_sync_with_resolution_error(RuntimeError("config.yaml could not be parsed"))

    reply = result["final_response"]
    assert "authentication" not in reply.lower()
    assert "重新登录" not in reply
    assert "config.yaml" not in reply
    assert result["api_calls"] == 0


def test_branded_auth_resolution_error_tells_user_to_relogin(branded):
    result = _run_sync_with_resolution_error(_StatusError("unauthorized", 401))

    assert _is_relogin_reply(result["final_response"])


def test_stock_resolution_error_wording_unchanged(stock):
    result = _run_sync_with_resolution_error(
        RuntimeError("No credentials found for provider 'nous'")
    )

    assert result["final_response"] == (
        "⚠️ Provider authentication failed: No credentials found for provider 'nous'"
    )


# --- exceptions escaping the agent turn (e.g. policy preload at init) --------


def test_branded_redaction_policy_401_tells_user_to_relogin(branded):
    exc = RedactionPolicyUnavailable(
        "登录凭证已失效（HTTP 401），请退出登录后重新登录。", reason="unauthorized"
    )

    assert _is_relogin_reply(gateway_run._branded_agent_exception_reply(exc))


def test_branded_wrapped_provider_401_tells_user_to_relogin(branded):
    try:
        try:
            raise _StatusError("invalid api key", 401)
        except _StatusError as inner:
            raise RuntimeError("agent init failed") from inner
    except RuntimeError as outer:
        exc = outer

    assert _is_relogin_reply(gateway_run._branded_agent_exception_reply(exc))


def test_branded_other_redaction_policy_failures_surface_their_reason(branded):
    exc = RedactionPolicyUnavailable("连接网关超时：timed out。", reason="timeout")

    reply = gateway_run._branded_agent_exception_reply(exc)

    assert "连接网关超时" in reply
    assert not _is_relogin_reply(reply)


def test_branded_403_is_not_treated_as_expired_login(branded):
    assert gateway_run._branded_agent_exception_reply(_StatusError("model not allowed", 403)) is None


def test_stock_agent_exception_keeps_upstream_handling(stock):
    exc = RedactionPolicyUnavailable("x", reason="unauthorized")

    assert gateway_run._branded_agent_exception_reply(exc) is None
