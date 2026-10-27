from types import SimpleNamespace

import pytest
from main import check_startup_config
from services.dingtalk_identity import configured


def settings(**changes):
    values = dict(jwt_secret_key="not-the-default", jwt_refresh_secret_key="also-not-default",
                  credential_encryption_key="set", provider_encryption_key="set", is_prod=True,
                  dingtalk_client_id="client", dingtalk_client_secret="secret",
                  dingtalk_corp_id="corp", dingtalk_redirect_uri="")
    values.update(changes)
    return SimpleNamespace(**values)


def test_directory_can_start_before_web_callback_is_registered():
    cfg = settings()
    check_startup_config(cfg)
    assert not configured(cfg)


def test_incomplete_directory_credentials_fail_production_startup():
    with pytest.raises(RuntimeError, match="通讯录配置不完整"):
        check_startup_config(settings(dingtalk_client_secret=""))


def test_unconfigured_dingtalk_preserves_password_only_startup():
    check_startup_config(settings(dingtalk_client_id="", dingtalk_client_secret="", dingtalk_corp_id=""))


def test_web_login_requires_valid_complete_https_callback():
    cfg = settings(dingtalk_redirect_uri="https://gateway.example.invalid/admin/user/dingtalk/callback")
    check_startup_config(cfg)
    assert configured(cfg)
    with pytest.raises(RuntimeError, match="网页登录"):
        check_startup_config(settings(dingtalk_redirect_uri="http://127.0.0.1:8765/callback"))
