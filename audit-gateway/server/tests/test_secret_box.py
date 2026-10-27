"""两套密码存储逻辑的测试（TODO 决策 #5）

关键断言：
- 本系统密码不可逆（bcrypt）
- 中台凭证可还原（Fernet），且不同用途的密钥互不通用
- 密钥轮换后旧密文仍可解密
"""

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def keys(monkeypatch):
    """给两种用途各注入一把独立密钥"""
    import config
    from utils import secret_box

    cred_key = Fernet.generate_key().decode()
    prov_key = Fernet.generate_key().decode()

    config.get_settings.cache_clear()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", cred_key)
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", prov_key)
    secret_box.reset_key_cache()

    yield {"credential": cred_key, "provider": prov_key}

    config.get_settings.cache_clear()
    secret_box.reset_key_cache()


def test_system_password_is_one_way():
    """本系统登录密码必须是不可逆哈希，且同一明文两次哈希结果不同（含盐）"""
    from utils.crypt import check_password, hash_password

    hashed = hash_password("P@ssw0rd")
    assert hashed != "P@ssw0rd"
    assert check_password("P@ssw0rd", hashed)
    assert not check_password("wrong", hashed)
    assert hash_password("P@ssw0rd") != hashed


def test_credential_roundtrip(keys):
    """中台密码必须能还原明文（要代登）"""
    from utils.secret_box import (SecretPurpose, decrypt_secret,
                                  encrypt_secret)

    plain = "中台密码-123!"
    cipher = encrypt_secret(plain, SecretPurpose.CREDENTIAL)
    assert cipher != plain
    assert decrypt_secret(cipher, SecretPurpose.CREDENTIAL) == plain


def test_purposes_use_separate_keys(keys):
    """用凭证密钥加密的内容，不能被上游 Key 的密钥解开"""
    from utils.secret_box import (SecretDecryptError, SecretPurpose,
                                  decrypt_secret, encrypt_secret)

    cipher = encrypt_secret("secret", SecretPurpose.CREDENTIAL)
    with pytest.raises(SecretDecryptError):
        decrypt_secret(cipher, SecretPurpose.PROVIDER)


def test_key_rotation_keeps_old_ciphertext_readable(keys, monkeypatch):
    """轮换主密钥后，把旧密钥挂到 *_KEYS_OLD，历史数据仍要能解开"""
    import config
    from utils import secret_box
    from utils.secret_box import (SecretPurpose, decrypt_secret,
                                  encrypt_secret, rotate_secret)

    old_cipher = encrypt_secret("旧数据", SecretPurpose.CREDENTIAL)

    new_key = Fernet.generate_key().decode()
    config.get_settings.cache_clear()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS_OLD", keys["credential"])
    secret_box.reset_key_cache()

    assert decrypt_secret(old_cipher, SecretPurpose.CREDENTIAL) == "旧数据"

    # 重加密后应由新密钥承载
    rotated = rotate_secret(old_cipher, SecretPurpose.CREDENTIAL)
    assert rotated != old_cipher
    assert decrypt_secret(rotated, SecretPurpose.CREDENTIAL) == "旧数据"


def test_missing_key_raises_clear_error(monkeypatch):
    """没配密钥时要给出可操作的报错，而不是默默用弱默认值"""
    import config
    from types import SimpleNamespace
    from utils import secret_box
    from utils.secret_box import SecretKeyMissing, SecretPurpose

    config.get_settings.cache_clear()
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    # 本机可能存在 .env/.env.dev；这个单测要验证的是「配置缺失」语义，
    # 不能被开发者本地文件悄悄补上一把真实密钥。
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            credential_encryption_key=None,
            credential_encryption_keys_old=None,
        ),
    )
    secret_box.reset_key_cache()

    with pytest.raises(SecretKeyMissing) as exc:
        secret_box.encrypt_secret("x", SecretPurpose.CREDENTIAL)
    assert "CREDENTIAL_ENCRYPTION_KEY" in str(exc.value)

    secret_box.reset_key_cache()


def test_mask_secret():
    from utils.secret_box import mask_secret

    assert mask_secret("sk-1234567890") == "sk-1*********"
    assert mask_secret("abc") == "***"
    assert mask_secret("") == ""
