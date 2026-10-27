"""网关签发的 API Key

格式：`sk-llano-<40 位随机>`，前 20 位（含前缀）单独存一列做索引定位。

为什么用 sha256 而不是 bcrypt：
- 用户密码用 bcrypt 是为了扛离线爆破，因为人选的密码熵很低
- API Key 是 40 位密码学随机（约 206 bit 熵），爆破不现实，
  而每个转发请求都要校验一次，bcrypt 那点故意的慢在这条热路径上是纯损耗

存哈希不存明文：明文只在签发响应里回显一次，丢了只能重发。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Tuple

KEY_PREFIX = "sk-llano-"
KEY_RANDOM_LENGTH = 40
# 前缀列长度：够区分不同 Key，又不至于泄露太多明文
PREFIX_STORE_LENGTH = 20

_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def generate_api_key() -> Tuple[str, str, str]:
    """生成一把新 Key

    返回 (明文, 存库用的前缀, sha256 哈希)
    """
    body = "".join(secrets.choice(_ALPHABET) for _ in range(KEY_RANDOM_LENGTH))
    plain = f"{KEY_PREFIX}{body}"
    return plain, key_prefix_of(plain), hash_api_key(plain)


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def key_prefix_of(plain: str) -> str:
    """取出用于索引定位的前缀"""
    return plain[:PREFIX_STORE_LENGTH]


def verify_api_key(plain: str, stored_hash: str) -> bool:
    """常数时间比对，避免时序侧信道"""
    return hmac.compare_digest(hash_api_key(plain), stored_hash)


def looks_like_api_key(token: str) -> bool:
    """粗筛：转发入口据此决定按 Key 还是按 JWT 解析"""
    return bool(token) and token.startswith(KEY_PREFIX)


def mask_api_key(plain_or_prefix: str) -> str:
    """展示用掩码，如 sk-llano-abcd****"""
    if not plain_or_prefix:
        return ""
    head = plain_or_prefix[:len(KEY_PREFIX) + 4]
    return f"{head}{'*' * 8}"
