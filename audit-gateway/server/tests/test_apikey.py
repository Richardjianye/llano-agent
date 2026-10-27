"""API Key 生成与校验测试"""

from utils.apikey import (KEY_PREFIX, generate_api_key, hash_api_key,
                          key_prefix_of, looks_like_api_key, mask_api_key,
                          verify_api_key)


def test_generated_key_shape():
    plain, prefix, key_hash = generate_api_key()
    assert plain.startswith(KEY_PREFIX)
    assert prefix == plain[:20]
    assert len(key_hash) == 64  # sha256 hex


def test_keys_are_unique():
    keys = {generate_api_key()[0] for _ in range(200)}
    assert len(keys) == 200


def test_verify_accepts_only_the_right_key():
    plain, _, key_hash = generate_api_key()
    other, _, _ = generate_api_key()

    assert verify_api_key(plain, key_hash) is True
    assert verify_api_key(other, key_hash) is False
    assert verify_api_key(plain + "x", key_hash) is False


def test_hash_is_deterministic():
    plain, _, key_hash = generate_api_key()
    assert hash_api_key(plain) == key_hash


def test_prefix_is_stable_and_short_enough():
    plain, prefix, _ = generate_api_key()
    assert key_prefix_of(plain) == prefix
    # 前缀只用于定位，不能长到把明文泄露出去
    assert len(prefix) < len(plain)


def test_looks_like_api_key_distinguishes_from_jwt():
    """转发入口靠这个判断走 Key 还是走 JWT 解析"""
    plain, _, _ = generate_api_key()
    jwt_like = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhYmMifQ.sig"

    assert looks_like_api_key(plain) is True
    assert looks_like_api_key(jwt_like) is False
    assert looks_like_api_key("") is False


def test_mask_hides_the_body():
    plain, _, _ = generate_api_key()
    masked = mask_api_key(plain)
    assert masked.startswith(KEY_PREFIX)
    assert len(masked) < len(plain)
    # 掩码后不能还原出完整明文
    assert plain not in masked
