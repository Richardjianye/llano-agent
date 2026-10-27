"""客户端侧更新清单验签

两组不同性质的测试：

1. **固定向量**：钉住 canonical_manifest_bytes 的字节输出，与网关那份共用同
   一个 release/manifest-vectors.json。
2. **源码共享区比对**：直接比两份实现的源码文本。向量只覆盖序列化，
   normalize / parse_version / manifest_applies_to 漂了向量是看不出来的，
   而这些函数漂了同样会让客户端对更新做出与网关不一致的判断。

两份代码同时失效的后果是全量客户端更新不上，所以这里刻意做重了一点。
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
RELEASE_DIR = os.path.join(REPO_ROOT, "audit-gateway", "release")
VECTORS_PATH = os.path.join(RELEASE_DIR, "manifest-vectors.json")

pytestmark = pytest.mark.skipif(
    not os.path.exists(VECTORS_PATH),
    reason="固定向量随网关项目一起走；只有 Hermes 树时跳过",
)

from hermes_cli.update_manifest import (  # noqa: E402
    POLICY_OPTIONAL, POLICY_REQUIRED, SIGNING_DOMAIN, ManifestError,
    UpdateSignatureError, builtin_public_key, canonical_manifest_bytes,
    manifest_applies_to, normalize_manifest, parse_version,
    require_trusted_manifest, verify_manifest, version_at_least)

with open(VECTORS_PATH, encoding="utf-8") as _fh:
    VECTORS = json.load(_fh)

PUBLIC_KEY = VECTORS["test_public_key"]
CASES = VECTORS["cases"]


def _manifest(index: int = 0) -> dict:
    return dict(CASES[index]["manifest"])


# ==================== 与网关的一致性 ====================


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_客户端算出的规范字节与固定向量一致(case):
    assert canonical_manifest_bytes(case["manifest"]).decode("utf-8") == case["canonical"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_客户端能验通过网关侧签出的签名(case):
    assert verify_manifest(case["manifest"], case["signature"], PUBLIC_KEY) is True


def test_共享区源码与网关那份逐字一致():
    """两份实现之间没有 import 关系，只能靠比源码挡住它们悄悄漂开。"""
    sys.path.insert(0, RELEASE_DIR)
    try:
        from shared_region import CLIENT_FILE, GATEWAY_FILE, shared_region
    finally:
        sys.path.remove(RELEASE_DIR)

    gateway = shared_region(GATEWAY_FILE)
    client = shared_region(CLIENT_FILE)
    if gateway != client:
        import difflib
        diff = "\n".join(
            difflib.unified_diff(
                gateway.splitlines(), client.splitlines(),
                "网关 release_manifest.py", "客户端 update_manifest.py", lineterm="",
            )
        )
        pytest.fail(f"更新清单共享区已经漂开，两边必须同步修改：\n{diff}")


def test_客户端没有签名能力():
    """客户端只有公钥。给它签名函数等于把私钥的作用发到每台机器上。"""
    import hermes_cli.update_manifest as mod

    assert not hasattr(mod, "sign_manifest")
    assert "Ed25519PrivateKey" not in open(mod.__file__, encoding="utf-8").read()


def test_签名域与固定向量一致():
    assert SIGNING_DOMAIN.decode() == VECTORS["signing_domain"]


# ==================== 信任根 ====================


def test_没有内置公钥时拒绝安装而不是跳过校验(monkeypatch):
    monkeypatch.delenv("HERMES_UPDATE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.update_manifest._BUILTIN_UPDATE_PUBLIC_KEY", "", raising=False
    )
    assert builtin_public_key() == ""
    with pytest.raises(UpdateSignatureError):
        require_trusted_manifest(_manifest(), CASES[0]["signature"])


def test_正式构建里环境变量改不动信任根(monkeypatch):
    """能设环境变量的人不该能替换更新公钥，否则签名形同虚设。"""
    monkeypatch.setattr(
        "hermes_cli.update_manifest._BUILTIN_UPDATE_PUBLIC_KEY", PUBLIC_KEY, raising=False
    )
    monkeypatch.setenv("HERMES_UPDATE_PUBLIC_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    assert builtin_public_key() == PUBLIC_KEY


def test_源码树里环境变量可用于开发(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_manifest._BUILTIN_UPDATE_PUBLIC_KEY", "", raising=False
    )
    monkeypatch.setenv("HERMES_UPDATE_PUBLIC_KEY", f"  {PUBLIC_KEY}  ")
    assert builtin_public_key() == PUBLIC_KEY


def test_验签通过返回归一化清单(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_manifest._BUILTIN_UPDATE_PUBLIC_KEY", PUBLIC_KEY, raising=False
    )
    shaped = require_trusted_manifest(_manifest(), CASES[0]["signature"])
    assert shaped["version"] == "1.4.0"
    assert set(shaped) == set(normalize_manifest(_manifest()))


def test_被篡改的清单拒绝安装(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_manifest._BUILTIN_UPDATE_PUBLIC_KEY", PUBLIC_KEY, raising=False
    )
    manifest = _manifest()
    manifest["artifact_url"] = "https://evil.example.com/x.dmg"
    with pytest.raises(UpdateSignatureError):
        require_trusted_manifest(manifest, CASES[0]["signature"])


def test_换一把公钥签的清单拒绝安装(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey

    attacker = Ed25519PrivateKey.generate()
    manifest = normalize_manifest(_manifest())
    body = canonical_manifest_bytes(manifest)
    forged = base64.b64encode(attacker.sign(body)).decode()

    monkeypatch.setattr(
        "hermes_cli.update_manifest._BUILTIN_UPDATE_PUBLIC_KEY", PUBLIC_KEY, raising=False
    )
    with pytest.raises(UpdateSignatureError):
        require_trusted_manifest(manifest, forged)


# ==================== 判定逻辑（与网关同规则）====================


def test_平台架构不符不算适用():
    assert manifest_applies_to(
        _manifest(), platform="win32", arch="x64", current_version="1.0.0"
    ) is False


def test_同版本不算有更新():
    assert manifest_applies_to(
        _manifest(), platform="darwin", arch="arm64", current_version="1.4.0"
    ) is False


def test_版本比较按数字():
    assert parse_version("1.2") == parse_version("1.2.0.0")
    assert version_at_least("1.10.0", "1.9.9") is True


def test_非法版本号被拒():
    with pytest.raises(ManifestError):
        parse_version("1.4.0-rc1")


def test_策略与最低版本共同决定强制():
    from hermes_cli.update_manifest import update_is_mandatory

    manifest = _manifest()
    assert manifest["policy"] == POLICY_REQUIRED
    assert update_is_mandatory(manifest, "1.3.0") is True

    manifest["policy"] = POLICY_OPTIONAL
    assert update_is_mandatory(manifest, "1.3.0") is False
    assert update_is_mandatory(manifest, "1.1.0") is True
