"""更新清单签名核心的测试

最重要的一组是「固定向量」：它钉住 canonical_manifest_bytes 的逐字节输出。
这个函数一旦漂了，**所有已发布的签名会同时失效，全量客户端一起更新失败**，
而且线上看到的现象是「签名验证不通过」，很容易被误判成密钥配错。
所以这里既比签名结果，也比 canonical 字节本身——后者在出问题时能一眼看出
是哪个字符变了。
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest
from services.release_manifest import (POLICY_OPTIONAL, POLICY_REQUIRED,
                                       SIGNING_DOMAIN, ManifestError,
                                       canonical_manifest_bytes,
                                       manifest_applies_to, normalize_manifest,
                                       parse_version, public_key_fingerprint,
                                       sign_manifest, update_is_mandatory,
                                       verify_manifest, version_at_least)

VECTORS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "release", "manifest-vectors.json"
)

with open(VECTORS_PATH, encoding="utf-8") as _fh:
    VECTORS = json.load(_fh)

PUBLIC_KEY = VECTORS["test_public_key"]
PRIVATE_KEY = VECTORS["test_private_key"]
CASES = VECTORS["cases"]


def _manifest(index: int = 0) -> dict:
    return dict(CASES[index]["manifest"])


# ==================== 与客户端那份实现的一致性 ====================


def test_共享区源码与客户端那份逐字一致():
    """两份实现分属两个部署单元，没有 import 关系，只能比源码。

    网关允许被单独部署（只拷 audit-gateway/server 到服务器上），那种情况下
    客户端文件不存在，跳过是对的——此时这台机器上根本没有会漂的第二份实现。
    """
    release_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "release")
    if not os.path.exists(os.path.join(release_dir, "shared_region.py")):
        pytest.skip("独立部署的网关没有带上 release/ 工具目录")

    sys.path.insert(0, release_dir)
    try:
        from shared_region import CLIENT_FILE, GATEWAY_FILE, shared_region
    finally:
        sys.path.remove(release_dir)

    if not os.path.exists(CLIENT_FILE):
        pytest.skip("当前树里没有客户端实现")

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


def test_网关持有签名能力而客户端不该有():
    """私钥只在离线签名机上，但签名函数本身放在网关侧的模块里给工具复用；
    客户端那份必须没有它。"""
    from services import release_manifest

    assert hasattr(release_manifest, "sign_manifest")


# ==================== 固定向量 ====================


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_固定向量的规范字节没有漂(case):
    assert canonical_manifest_bytes(case["manifest"]).decode("utf-8") == case["canonical"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_固定向量的签名验得过(case):
    assert verify_manifest(case["manifest"], case["signature"], PUBLIC_KEY) is True


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_重新签名能复现固定向量里的签名(case):
    assert sign_manifest(case["manifest"], PRIVATE_KEY) == case["signature"]


def test_签名域进了待签名字节():
    """裸 JSON 的签名不能被当成清单签名。"""
    manifest = _manifest()
    body = canonical_manifest_bytes(manifest)
    assert body.startswith(SIGNING_DOMAIN + b"\n")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(PRIVATE_KEY))
    naked = base64.b64encode(key.sign(body[len(SIGNING_DOMAIN) + 1:])).decode()
    assert verify_manifest(manifest, naked, PUBLIC_KEY) is False


# ==================== 篡改 ====================


@pytest.mark.parametrize(
    "field, tampered",
    [
        ("version", "9.9.9"),
        ("platform", "win32"),
        ("arch", "x64"),
        ("policy", POLICY_OPTIONAL),
        ("min_supported_version", "1.3.0"),
        ("artifact_url", "https://evil.example.com/x.dmg"),
        ("artifact_sha256", "b" * 64),
        ("artifact_size", 182452225),
        ("release_notes", "被改过的发行说明"),
        ("released_at", "2026-08-25T02:30:00Z"),
    ],
)
def test_任何一个签名字段被改都验不过(field, tampered):
    manifest = _manifest()
    assert manifest[field] != tampered, "用例本身写错了：篡改值和原值相同"
    manifest[field] = tampered
    assert verify_manifest(manifest, CASES[0]["signature"], PUBLIC_KEY) is False


def test_换一把公钥验不过():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey
    other = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode()
    assert verify_manifest(_manifest(), CASES[0]["signature"], other) is False


@pytest.mark.parametrize(
    "signature",
    ["", None, "不是 base64", "AAAA", base64.b64encode(b"\x00" * 64).decode()],
)
def test_烂签名一律返回False而不是抛(signature):
    assert verify_manifest(_manifest(), signature, PUBLIC_KEY) is False


@pytest.mark.parametrize("public_key", ["", None, "zzz", base64.b64encode(b"\x01" * 31).decode()])
def test_公钥缺失或长度不对也返回False(public_key):
    assert verify_manifest(_manifest(), CASES[0]["signature"], public_key) is False


def test_清单字段不合法时验签返回False而不是抛():
    manifest = _manifest()
    manifest["artifact_size"] = -1
    assert verify_manifest(manifest, CASES[0]["signature"], PUBLIC_KEY) is False


# ==================== 归一化 ====================


def test_归一化是幂等的():
    """签名方签的是归一化结果，验签方会再归一化一次；不幂等就会随机验不过。"""
    once = normalize_manifest(_manifest())
    assert normalize_manifest(once) == once


def test_归一化会丢掉未知字段以便将来加展示字段():
    manifest = _manifest()
    manifest["ui_badge"] = "热门"
    assert "ui_badge" not in normalize_manifest(manifest)
    assert verify_manifest(manifest, CASES[0]["signature"], PUBLIC_KEY) is True


def test_字段顺序不影响规范字节():
    manifest = _manifest()
    reordered = dict(reversed(list(manifest.items())))
    assert canonical_manifest_bytes(reordered) == canonical_manifest_bytes(manifest)


def test_大小写与空白被归一化后仍然验得过():
    manifest = _manifest()
    manifest["platform"] = " DARWIN "
    manifest["arch"] = "ARM64"
    manifest["policy"] = "Required"
    manifest["artifact_sha256"] = manifest["artifact_sha256"].upper()
    assert verify_manifest(manifest, CASES[0]["signature"], PUBLIC_KEY) is True


@pytest.mark.parametrize(
    "field, value",
    [
        ("platform", "linux"),
        ("arch", "riscv"),
        ("policy", "silent"),
        ("artifact_url", "ftp://gw/x.dmg"),
        ("artifact_sha256", "z" * 64),
        ("artifact_sha256", "a" * 63),
        ("artifact_size", 0),
        ("artifact_size", "182452224"),
        ("released_at", "2026-08-24 02:30:00"),
        ("released_at", "2026-08-24T02:30:00+08:00"),
        ("version", "1.4.0-rc1"),
        ("version", "v1.4.0"),
        ("version", "1.2.3.4.5"),
    ],
)
def test_非法字段被拒绝(field, value):
    manifest = _manifest()
    manifest[field] = value
    with pytest.raises(ManifestError):
        normalize_manifest(manifest)


def test_artifact_size为True不能混成1():
    manifest = _manifest()
    manifest["artifact_size"] = True
    with pytest.raises(ManifestError):
        normalize_manifest(manifest)


def test_最低可用版本不能高于本版本():
    """否则刚装上的新版本会立刻把自己判成过期，客户端陷入更新死循环。"""
    manifest = _manifest()
    manifest["min_supported_version"] = "9.0.0"
    with pytest.raises(ManifestError):
        normalize_manifest(manifest)


def test_不支持的schema被拒():
    manifest = _manifest()
    manifest["schema"] = 2
    with pytest.raises(ManifestError):
        normalize_manifest(manifest)


def test_发行说明超长被拒():
    manifest = _manifest()
    manifest["release_notes"] = "长" * 4001
    with pytest.raises(ManifestError):
        normalize_manifest(manifest)


# ==================== 版本比较 ====================


def test_版本号补齐后同一版本的两种写法相等():
    assert parse_version("1.2") == parse_version("1.2.0.0")


@pytest.mark.parametrize(
    "low, high",
    [("1.2.0", "1.10.0"), ("1.9.9", "2.0"), ("1.2.3", "1.2.3.1"), ("0.1", "1.0")],
)
def test_版本比较按数字而不是字典序(low, high):
    assert parse_version(low) < parse_version(high)
    assert version_at_least(high, low) is True
    assert version_at_least(low, high) is False


def test_同版本算达标():
    assert version_at_least("1.4.0", "1.4.0") is True


# ==================== 适用判定 ====================


def test_同版本不算有更新():
    assert manifest_applies_to(
        _manifest(), platform="darwin", arch="arm64", current_version="1.4.0"
    ) is False


def test_更旧的版本才算有更新():
    assert manifest_applies_to(
        _manifest(), platform="darwin", arch="arm64", current_version="1.3.9"
    ) is True


def test_更新的客户端不会被推回旧版():
    assert manifest_applies_to(
        _manifest(), platform="darwin", arch="arm64", current_version="1.5.0"
    ) is False


@pytest.mark.parametrize(
    "platform, arch", [("win32", "arm64"), ("darwin", "x64"), ("win32", "x64")]
)
def test_平台或架构对不上一律不适用(platform, arch):
    assert manifest_applies_to(
        _manifest(), platform=platform, arch=arch, current_version="1.0.0"
    ) is False


def test_清单本身不合法时不适用而不是抛():
    manifest = _manifest()
    manifest["platform"] = "linux"
    assert manifest_applies_to(
        manifest, platform="linux", arch="arm64", current_version="1.0.0"
    ) is False


# ==================== 强制更新判定 ====================


def test_策略写required就强制():
    manifest = _manifest()
    assert manifest["policy"] == POLICY_REQUIRED
    assert update_is_mandatory(manifest, "1.3.0") is True


def test_策略optional且版本达标时不强制():
    manifest = _manifest()
    manifest["policy"] = POLICY_OPTIONAL
    assert update_is_mandatory(manifest, "1.3.0") is False


def test_策略optional但低于最低可用版本仍然强制():
    """这条是为了能对**已经发出去的旧版本**追加强制，而不必重发一版。"""
    manifest = _manifest()
    manifest["policy"] = POLICY_OPTIONAL
    assert manifest["min_supported_version"] == "1.2.0"
    assert update_is_mandatory(manifest, "1.1.9") is True


def test_恰好等于最低可用版本不强制():
    manifest = _manifest()
    manifest["policy"] = POLICY_OPTIONAL
    assert update_is_mandatory(manifest, "1.2.0") is False


# ==================== 公钥指纹 ====================


def test_公钥指纹稳定():
    assert public_key_fingerprint(PUBLIC_KEY) == public_key_fingerprint(PUBLIC_KEY)
    assert len(public_key_fingerprint(PUBLIC_KEY)) == 16


@pytest.mark.parametrize(
    "bad",
    [
        "不是密钥",
        "",
        None,
        # 合法 base64，但只有 3 字节。指纹函数不经过 cryptography 建密钥对象，
        # 少了长度检查就会给一段垃圾算出一个看着挺像样的指纹。
        "None",
        base64.b64encode(b"\x01" * 31).decode(),
        base64.b64encode(b"\x01" * 33).decode(),
    ],
)
def test_不是32字节的东西算不出指纹(bad):
    assert public_key_fingerprint(bad) is None
