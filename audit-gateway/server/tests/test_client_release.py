"""版本发布接口与客户端更新检查

这一层要保证的核心只有一句：**网关不能成为伪造更新的通道**。所以测的重点是
「什么情况下拒绝」而不是「顺利路径能跑通」——顺利路径只要一条，拒绝路径要
穷举：没配公钥、签名对不上、清单被改、库里的行被直接改。

最后一组是端到端的往返：发布接口收下的清单，经过 JSON 入库、HTTP 下发之后，
必须还能被**客户端那份实现**验签通过。中间任何一步改了字段类型或格式，
签名就对不上，而线上现象是全量客户端更新失败。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from model.response import UserInfo
from router.admin import router as admin_router
from router.v1 import router as v1_router
from services.release_manifest import (normalize_manifest,
                                       public_key_fingerprint, sign_manifest)
from utils.dependencies import require_login
from utils.middleware import register_exception_handlers

VECTORS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "release", "manifest-vectors.json"
)
with open(VECTORS_PATH, encoding="utf-8") as _fh:
    VECTORS = json.load(_fh)

PUBLIC_KEY = VECTORS["test_public_key"]
PRIVATE_KEY = VECTORS["test_private_key"]
MANIFEST = VECTORS["cases"][0]["manifest"]
SIGNATURE = VECTORS["cases"][0]["signature"]


@pytest.fixture(autouse=True)
def _public_key(monkeypatch):
    import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("UPDATE_PUBLIC_KEY", PUBLIC_KEY)
    yield
    config.get_settings.cache_clear()


def _app(*, role: str = "president") -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_router)
    app.include_router(v1_router)
    app.dependency_overrides[require_login] = lambda: UserInfo(
        id=1, uid="president-1", username="admin", permission=role,
        dept_id=1, dept_code="exec",
    )
    return app


def _async(fn):
    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def _row(manifest=None, signature=SIGNATURE, **overrides):
    shaped = normalize_manifest(manifest or MANIFEST)
    row = SimpleNamespace(
        id=1,
        manifest_json=json.dumps(
            shaped, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ),
        signature=signature,
        public_key_fp=public_key_fingerprint(PUBLIC_KEY),
        status=1,
        published_by="president-1",
        created_at=None,
        release_notes=shaped["release_notes"] or None,
        **{k: shaped[k] for k in (
            "version", "platform", "arch", "policy", "min_supported_version",
            "artifact_url", "artifact_sha256", "artifact_size", "released_at",
        )},
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


# ==================== 发布：拒绝路径 ====================


def test_没配公钥就发不了版而不是跳过校验(monkeypatch):
    import config

    config.get_settings.cache_clear()
    monkeypatch.delenv("UPDATE_PUBLIC_KEY", raising=False)
    captured = []
    monkeypatch.setattr(
        "router.admin.release.insert_release",
        _async(lambda **kw: captured.append(kw)),
    )

    monkeypatch.setattr(
        "router.admin.release.get_release_by_target", _async(lambda *a, **k: None)
    )

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": MANIFEST, "signature": SIGNATURE}
    ).json()

    assert body["code"] != 0
    assert captured == [], "验不了签名的网关不该往库里写任何东西"
    # 没配公钥和签名对不上都会拒绝，但**诊断方向完全不同**：一个是去补配置，
    # 一个是去查签名机的密钥。少了这条断言，把「未配置公钥」那道判断删掉测试
    # 依然全绿——验签本来也会失败——运维却只能看到「签名校验失败」。
    assert "UPDATE_PUBLIC_KEY" in body["message"]
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    "field, value",
    [
        ("artifact_url", "https://evil.example.com/x.dmg"),
        ("artifact_sha256", "b" * 64),
        ("version", "9.9.9"),
        ("policy", "optional"),
        ("min_supported_version", "1.3.0"),
        ("release_notes", "被改过的说明"),
    ],
)
def test_清单被改过就拒绝入库(monkeypatch, field, value):
    captured = []
    monkeypatch.setattr(
        "router.admin.release.insert_release", _async(lambda **kw: captured.append(kw))
    )
    tampered = dict(MANIFEST)
    tampered[field] = value

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": tampered, "signature": SIGNATURE}
    ).json()

    assert body["code"] != 0
    assert captured == []


def test_换一把私钥签的清单拒绝入库(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey

    attacker_key = base64.b64encode(
        Ed25519PrivateKey.generate().private_bytes_raw()
    ).decode()
    forged = sign_manifest(MANIFEST, attacker_key)

    captured = []
    monkeypatch.setattr(
        "router.admin.release.insert_release", _async(lambda **kw: captured.append(kw))
    )

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": MANIFEST, "signature": forged}
    ).json()

    assert body["code"] != 0
    assert captured == []


def test_清单字段不合法时拒绝并说明原因(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "router.admin.release.insert_release", _async(lambda **kw: captured.append(kw))
    )
    bad = dict(MANIFEST)
    bad["platform"] = "linux"

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": bad, "signature": SIGNATURE}
    ).json()

    assert body["code"] != 0
    assert "linux" in body["message"]
    assert captured == []


def test_同平台架构下版本号重复被拒(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "router.admin.release.get_release_by_target",
        _async(lambda *a, **k: _row()),
    )
    monkeypatch.setattr(
        "router.admin.release.insert_release", _async(lambda **kw: captured.append(kw))
    )

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": MANIFEST, "signature": SIGNATURE}
    ).json()

    assert body["code"] != 0
    assert captured == []


@pytest.mark.parametrize("role", ["employee", "manager", "auditor"])
def test_没有release_edit的角色都不能发布(monkeypatch, role):
    """manager / auditor 不是凑数的：它们有 usage:view 而没有 release:edit。

    只测 employee 是不够的——employee 几乎什么权限都没有，把接口的权限点错写成
    另一个权限（usage:view）时它照样会被拒，测试照样全绿。要挡住「权限点写错」
    这类错误，就得有一个「在别的权限下能过、在这个权限下不能过」的角色。

    挡不住的一种写错：把 release:edit 误写成 release:view。两个权限点目前授予
    的角色完全相同，行为上无法区分。view/edit 的拆分是给将来留的余地，此刻并
    没有真的分出两拨人——不为了让变异变红去凭空造一个角色。
    """
    captured = []
    monkeypatch.setattr(
        "router.admin.release.insert_release", _async(lambda **kw: captured.append(kw))
    )
    monkeypatch.setattr(
        "router.admin.release.get_release_by_target", _async(lambda *a, **k: None)
    )

    body = TestClient(_app(role=role)).post(
        "/admin/release/publish", json={"manifest": MANIFEST, "signature": SIGNATURE}
    ).json()

    assert body["code"] != 0
    assert captured == []


# ==================== 发布：顺利路径 ====================


def test_验签通过才入库且存的是归一化原文(monkeypatch):
    captured = {}

    async def _insert(**kwargs):
        captured.update(kwargs)
        return 42

    monkeypatch.setattr("router.admin.release.get_release_by_target", _async(lambda *a, **k: None))
    monkeypatch.setattr("router.admin.release.insert_release", _insert)

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": MANIFEST, "signature": SIGNATURE}
    ).json()

    assert body["code"] == 0
    assert body["data"]["id"] == 42
    # 入库的必须是 normalize 之后的结果，不是请求原样：客户端验的就是这一份
    assert captured["manifest"] == normalize_manifest(MANIFEST)
    assert captured["signature"] == SIGNATURE
    assert captured["public_key_fp"] == public_key_fingerprint(PUBLIC_KEY)
    assert captured["published_by"] == "president-1"


def test_大小写不同的清单归一化后仍能发布(monkeypatch):
    """签名机签的是归一化结果；请求里字段大小写不同不该导致发布失败。"""
    captured = {}

    async def _insert(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr("router.admin.release.get_release_by_target", _async(lambda *a, **k: None))
    monkeypatch.setattr("router.admin.release.insert_release", _insert)

    loose = dict(MANIFEST)
    loose["platform"] = "DARWIN"
    loose["artifact_sha256"] = loose["artifact_sha256"].upper()

    body = TestClient(_app()).post(
        "/admin/release/publish", json={"manifest": loose, "signature": SIGNATURE}
    ).json()

    assert body["code"] == 0
    assert captured["manifest"]["platform"] == "darwin"


# ==================== 客户端更新检查 ====================


def _client(monkeypatch, row):
    monkeypatch.setattr(
        "router.v1.client_update.latest_release_for", _async(lambda *a, **k: row)
    )
    return TestClient(_app())


def test_有更新时返回清单与签名(monkeypatch):
    response = _client(monkeypatch, _row()).get(
        "/v1/client-update", params={"platform": "darwin", "arch": "arm64", "version": "1.3.0"}
    )

    update = response.json()["update"]
    assert update["manifest"]["version"] == "1.4.0"
    assert update["signature"] == SIGNATURE


def test_不下发网关自己算出来的强制标记(monkeypatch):
    """没进签名的布尔值可以被中间人改。客户端必须从已验签的清单里自己算。"""
    update = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.3.0"},
    ).json()["update"]

    assert set(update) == {"manifest", "signature"}
    assert "mandatory" not in update["manifest"]
    assert "required" not in update


def test_同版本没有更新(monkeypatch):
    body = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.4.0"},
    ).json()

    assert body["update"] is None


def test_更高版本不会被推回旧版(monkeypatch):
    body = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.9.0"},
    ).json()

    assert body["update"] is None


def test_没有任何已发布版本时返回无更新(monkeypatch):
    body = _client(monkeypatch, None).get(
        "/v1/client-update",
        params={"platform": "win32", "arch": "x64", "version": "1.0.0"},
    ).json()

    assert body["update"] is None


def test_清单平台与请求不符时不下发(monkeypatch):
    """库里查出来的行万一平台对不上（改库、或查询条件写错），也不能发出去 ——
    装错架构的包比不更新糟得多。"""
    body = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "win32", "arch": "x64", "version": "1.0.0"},
    ).json()

    assert body["update"] is None


def test_库里的签名被改过就不下发(monkeypatch):
    """拦不住有私钥的人，作用是让问题出现在网关日志里而不是两百台机器上。"""
    body = _client(
        monkeypatch, _row(signature=base64.b64encode(b"\x00" * 64).decode())
    ).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.0.0"},
    ).json()

    assert body["update"] is None


def test_网关公钥被换掉后不下发(monkeypatch):
    import config
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey

    config.get_settings.cache_clear()
    monkeypatch.setenv(
        "UPDATE_PUBLIC_KEY",
        base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        ).decode(),
    )

    body = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.0.0"},
    ).json()

    assert body["update"] is None
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    "params",
    [
        {"platform": "linux", "arch": "arm64", "version": "1.0.0"},
        {"platform": "darwin", "arch": "riscv", "version": "1.0.0"},
        {"platform": "darwin", "arch": "arm64", "version": "1.4.0-rc1"},
        {"platform": "darwin", "arch": "arm64", "version": "v1"},
    ],
)
def test_参数不合法明确报错而不是静默无更新(monkeypatch, params):
    """静默返回「没有更新」会让一个自身版本号坏掉的客户端永远停在旧版。"""
    body = _client(monkeypatch, _row()).get("/v1/client-update", params=params).json()

    assert body.get("code") not in (0, None) or "update" not in body


def test_不缓存更新检查结果(monkeypatch):
    response = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.0.0"},
    )

    assert response.headers["cache-control"] == "no-store"


# ==================== 端到端往返 ====================


def test_下发的清单能被客户端那份实现验签通过(monkeypatch):
    """入库、取出、HTTP 下发一整圈之后签名还得对得上。

    中间任何一步改了字段类型或格式（artifact_size 变成字符串、released_at
    被重新格式化）都会在这里红，而线上表现是全量客户端更新失败。
    """
    repo_root = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    )
    client_file = os.path.join(repo_root, "hermes_cli", "update_manifest.py")
    if not os.path.exists(client_file):
        pytest.skip("独立部署的网关树里没有客户端实现")

    # 按文件路径加载，不动 sys.path：仓库根目录下有个 utils.py，一旦把根目录
    # 插到 sys.path 最前面，网关自己的 utils 包就会被它顶掉，后面的用例会以
    # 「No module named 'utils.crypt'」这种毫不相干的样子失败。
    spec = importlib.util.spec_from_file_location("_client_update_manifest", client_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require_trusted_manifest = module.require_trusted_manifest

    update = _client(monkeypatch, _row()).get(
        "/v1/client-update",
        params={"platform": "darwin", "arch": "arm64", "version": "1.0.0"},
    ).json()["update"]

    shaped = require_trusted_manifest(
        update["manifest"], update["signature"], public_key=PUBLIC_KEY
    )
    assert shaped["version"] == "1.4.0"
    assert shaped["artifact_size"] == MANIFEST["artifact_size"]
