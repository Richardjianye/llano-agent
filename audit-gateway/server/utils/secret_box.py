"""可逆加密 —— 用于必须还原明文的外部凭证

与 utils/crypt.py 的分工（TODO 决策 #5，两套密码逻辑必须分开）：

    utils/crypt.py     本系统登录密码 → bcrypt 单向哈希，永不还原
    utils/secret_box.py 中台账号密码 / 上游 API Key → Fernet 可逆加密

用途不同的密钥必须分开配置，避免一把钥匙串起所有秘密：

    CREDENTIAL_ENCRYPTION_KEY  中台等外部系统凭证
    PROVIDER_ENCRYPTION_KEY    上游模型服务 API Key

密钥生成：
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

密钥轮换：把旧密钥追加到 *_ENCRYPTION_KEYS_OLD（逗号分隔），
新数据用主密钥加密，旧数据仍可解密，随后分批重加密并提升 key_version。
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretPurpose(str, Enum):
    """密钥用途 —— 每种用途一把独立密钥"""

    CREDENTIAL = "credential"   # 中台账号密码
    PROVIDER = "provider"       # 上游 API Key


class SecretKeyMissing(RuntimeError):
    """未配置对应用途的加密密钥"""


class SecretDecryptError(RuntimeError):
    """解密失败：密文损坏，或密钥已轮换且旧密钥未保留"""


def _key_config(purpose: SecretPurpose) -> tuple[Optional[str], List[str]]:
    """从配置读取 (主密钥, 旧密钥列表)"""
    from config import get_settings

    settings = get_settings()
    if purpose is SecretPurpose.CREDENTIAL:
        primary = settings.credential_encryption_key
        old_raw = settings.credential_encryption_keys_old
    else:
        primary = settings.provider_encryption_key
        old_raw = settings.provider_encryption_keys_old

    old = [k.strip() for k in (old_raw or "").split(",") if k.strip()]
    return (primary or None), old


@lru_cache
def _get_fernet(purpose: SecretPurpose) -> MultiFernet:
    """构建 MultiFernet：第一把用于加密，全部用于解密"""
    primary, old_keys = _key_config(purpose)
    if not primary:
        env_name = (
            "CREDENTIAL_ENCRYPTION_KEY"
            if purpose is SecretPurpose.CREDENTIAL
            else "PROVIDER_ENCRYPTION_KEY"
        )
        raise SecretKeyMissing(
            f"未配置 {env_name}。生成方式：python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    try:
        fernets = [Fernet(primary.encode())] + [Fernet(k.encode()) for k in old_keys]
    except (ValueError, TypeError) as exc:
        raise SecretKeyMissing(f"加密密钥格式非法（需为 Fernet base64 密钥）：{exc}") from exc

    return MultiFernet(fernets)


def encrypt_secret(plaintext: str, purpose: SecretPurpose) -> str:
    """加密，返回可直接入库的字符串"""
    if plaintext is None:
        raise ValueError("待加密内容不能为 None")
    return _get_fernet(purpose).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str, purpose: SecretPurpose) -> str:
    """解密。调用方有义务对中台凭证的解密写审计日志"""
    if not ciphertext:
        raise SecretDecryptError("密文为空")
    try:
        return _get_fernet(purpose).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptError("解密失败：密文损坏或密钥不匹配（检查是否已轮换密钥）") from exc


def rotate_secret(ciphertext: str, purpose: SecretPurpose) -> str:
    """用主密钥重新加密旧密文（密钥轮换时批量调用）"""
    return _get_fernet(purpose).rotate(ciphertext.encode("utf-8")).decode("utf-8")


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """脱敏展示，用于日志与接口返回，避免明文外泄"""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def reset_key_cache() -> None:
    """清空密钥缓存（改配置或测试时调用）"""
    _get_fernet.cache_clear()
