"""客户端更新清单：字段归一化、规范序列化、Ed25519 签名与验签

## 私钥不在网关上

发布流程是三段，**签名那一段不在服务器上发生**：

    离线签名机          发布者                网关
    ─────────           ──────                ────
    私钥 + 清单
      → 签名        ── 清单 + 签名 ──→   用公钥验一遍才入库
                                          （验得了，签不出来）

网关只持有公钥。这样即使网关被拿下，攻击者也签不出一份能被客户端接受的
清单——而签名的全部意义就是「即使分发通道被控制，客户端也能识别出这不是
我们发的」。把私钥放上来，这个性质就没了。

网关在发布时验一遍签名，不是为了防攻击（它没有能力阻止一个已经拿到私钥的
人），而是**防止发错**：清单和签名对不上时当场拒绝，不要等客户端升级失败
才发现。

## 两处实现必须逐字节一致

同一套规则存在于两个部署单元里：

    audit-gateway/server/services/release_manifest.py   网关：发布时验签
    hermes_cli/update_manifest.py                        客户端：下载时验签

`canonical_manifest_bytes()` 在两边产出的字节必须完全相同，差一个空格都会
让客户端把所有正常发布判成「签名失败」，且是**全量客户端同时失效**——这是
本模块最危险的失效模式。两边的测试各自钉住 `release/manifest-vectors.json`
里的固定向量，任何一边漂了都会红。改这个文件时，另一个文件必须同步改。

## 归一化必须幂等

签名方签的是归一化之后的字节，验签方拿到的清单会再归一化一次。若
`normalize_manifest(normalize_manifest(x)) != normalize_manifest(x)`，
验签就会随机失败。见 test_归一化是幂等的。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

# ==================== 共享区 开始 ====================
# 以下内容必须与 hermes_cli/update_manifest.py 逐字一致

# 签名域分隔：防止「某个别的地方签出来的 JSON」被当成清单签名重放。
# 改这个常量等于换一套签名体系，老客户端会全部验不过。
SIGNING_DOMAIN = b"llano-client-update-manifest-v1"

SCHEMA_VERSION = 1

POLICY_OPTIONAL = "optional"
POLICY_REQUIRED = "required"
POLICIES = frozenset({POLICY_OPTIONAL, POLICY_REQUIRED})

# 与 Electron 的 process.platform / process.arch 同名，客户端上报什么就是什么，
# 中间不做映射——映射表是又一处会漂的东西。
PLATFORMS = frozenset({"darwin", "win32"})
ARCHES = frozenset({"x64", "arm64"})

MANIFEST_FIELDS: Tuple[str, ...] = (
    "schema",
    "version",
    "platform",
    "arch",
    "policy",
    "min_supported_version",
    "artifact_url",
    "artifact_sha256",
    "artifact_size",
    "release_notes",
    "released_at",
)

# 版本号语法刻意收窄：只允许 1~4 段纯数字。
# 不支持 pre-release 后缀是有意的——「1.2.0-rc1 和 1.2.0 谁大」这类问题一旦
# 引入，判错的后果是客户端以为自己已是最新版而不再更新，属于静默失效。
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

RELEASE_NOTES_MAX_CHARS = 4000
ARTIFACT_SIZE_MAX = 4 * 1024 * 1024 * 1024


class ManifestError(ValueError):
    """清单字段不合法。只在发布/构造侧抛出，验签侧一律返回 False。"""


def _require_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"清单字段 {key} 缺失或不是非空字符串")
    return value.strip()


def parse_version(text: Any) -> Tuple[int, ...]:
    """把版本号解析成可比较的整数元组；不合法直接抛。

    补齐到 4 段，这样 `1.2` 和 `1.2.0.0` 比较结果相同——否则
    (1, 2) < (1, 2, 0) 会让「同一个版本的两种写法」比出大小。
    """
    value = str(text or "").strip()
    if not _VERSION_RE.match(value):
        raise ManifestError(f"版本号格式不合法：{text!r}（只接受 1.2 / 1.2.3 这类纯数字段）")
    parts = tuple(int(p) for p in value.split("."))
    return parts + (0,) * (4 - len(parts))


def version_at_least(current: Any, floor: Any) -> bool:
    """current >= floor。两边都必须是合法版本号。"""
    return parse_version(current) >= parse_version(floor)


def normalize_manifest(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """校验并归一化，返回**只含** MANIFEST_FIELDS 的新 dict。

    多余字段被丢弃而不是报错：签名覆盖的就是这里返回的这些字段，网关或客户端
    将来往响应里加展示用字段时，不应该让老签名失效。
    """
    schema = raw.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ManifestError(f"不支持的清单 schema：{schema!r}")

    version = _require_text(raw, "version")
    parse_version(version)  # 只为校验语法
    min_supported = _require_text(raw, "min_supported_version")
    if not version_at_least(version, min_supported):
        raise ManifestError(
            f"min_supported_version({min_supported}) 高于 version({version})，"
            "这会让刚装上的新版本立刻又被判为过期"
        )

    platform = _require_text(raw, "platform").lower()
    if platform not in PLATFORMS:
        raise ManifestError(f"不支持的平台：{platform!r}")

    arch = _require_text(raw, "arch").lower()
    if arch not in ARCHES:
        raise ManifestError(f"不支持的架构：{arch!r}")

    policy = _require_text(raw, "policy").lower()
    if policy not in POLICIES:
        raise ManifestError(f"不支持的更新策略：{policy!r}")

    artifact_url = _require_text(raw, "artifact_url")
    if not artifact_url.lower().startswith(("http://", "https://")):
        raise ManifestError("artifact_url 必须是 http(s) 地址")

    sha256 = _require_text(raw, "artifact_sha256").lower()
    if not _SHA256_RE.match(sha256):
        raise ManifestError("artifact_sha256 必须是 64 位小写十六进制")

    size = raw.get("artifact_size")
    # bool 是 int 的子类，True 会被当成 1 混过去
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ManifestError("artifact_size 必须是正整数")
    if size > ARTIFACT_SIZE_MAX:
        raise ManifestError("artifact_size 超出上限")

    released_at = _require_text(raw, "released_at")
    if not _RELEASED_AT_RE.match(released_at):
        # 全库其它时间戳是服务器本地时间（见 model/database.now），这一列是
        # 例外：它由离线签名机写入并进签名，机器时区不受我们控制，只能用 UTC。
        raise ManifestError("released_at 必须形如 2026-08-24T02:30:00Z（UTC）")

    notes = raw.get("release_notes") or ""
    if not isinstance(notes, str):
        raise ManifestError("release_notes 必须是字符串")
    notes = notes.strip()
    if len(notes) > RELEASE_NOTES_MAX_CHARS:
        raise ManifestError(f"release_notes 超过 {RELEASE_NOTES_MAX_CHARS} 字")

    return {
        "schema": SCHEMA_VERSION,
        "version": version,
        "platform": platform,
        "arch": arch,
        "policy": policy,
        "min_supported_version": min_supported,
        "artifact_url": artifact_url,
        "artifact_sha256": sha256,
        "artifact_size": size,
        "release_notes": notes,
        "released_at": released_at,
    }


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """待签名字节。**改这里等于让全量已发布签名失效。**

    sort_keys 保证字段顺序无关，separators 去掉所有可选空白，
    ensure_ascii=False 让中文发行说明按 UTF-8 原样进签名（而不是 \\uXXXX），
    三者共同决定「同一份清单只有一种字节表示」。
    """
    body = json.dumps(
        normalize_manifest(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return SIGNING_DOMAIN + b"\n" + body


def _decode_key(material: Any, expected: int) -> bytes:
    """base64 → 原始密钥字节。任何不合法输入都抛 ValueError。

    `or ""` 不是多余的：没有它时 None 会变成字符串 "None"，那恰好是 4 个合法
    base64 字符、能解出 3 字节——靠「解出来长度不对」兜住 None 是个巧合，不是设计。

    长度检查与 cryptography 自己的校验重叠（它对非 32 字节同样抛 ValueError），
    保留是因为 public_key_fingerprint 不经过 cryptography 建密钥对象，那里这
    一行是唯一挡住「给一段合法 base64 垃圾也算出指纹」的东西。
    """
    raw = base64.b64decode(str(material or "").strip(), validate=True)
    if len(raw) != expected:
        raise ValueError(f"密钥长度应为 {expected} 字节，实际 {len(raw)}")
    return raw


def verify_manifest(
    manifest: Mapping[str, Any],
    signature_b64: Any,
    public_key_b64: Any,
) -> bool:
    """验签。任何异常都收敛成 False —— 校验函数不该有「出错」这第三种结局。

    密钥缺失、base64 烂掉、清单字段不合法、签名对不上，对调用方来说是同一件
    事：**不能信任这份清单**。若把其中一部分放成异常，调用点就必须记得 try，
    而漏掉 try 的那一处会变成「异常向上冒泡后被当成别的错误吞掉」。

    这里曾经在 try 之前多写了一个 `if not public_key_b64 or not signature_b64`
    的早退。变异验证证明删掉它测试全绿——空值本来就会在 _decode_key 里抛出并
    被同一个 except 收成 False，两道判断互相遮蔽，留着只会让人以为空值走的是
    另一条路。
    """
    try:
        key = Ed25519PublicKey.from_public_bytes(_decode_key(str(public_key_b64), 32))
        signature = base64.b64decode(str(signature_b64).strip(), validate=True)
        key.verify(signature, canonical_manifest_bytes(manifest))
        return True
    except (InvalidSignature, ManifestError, ValueError, TypeError):
        return False


def manifest_applies_to(
    manifest: Mapping[str, Any],
    *,
    platform: str,
    arch: str,
    current_version: str,
) -> bool:
    """这份清单是否是「该机器应当安装的更新」。

    同版本不算更新；低于清单版本才算。平台/架构不符一律不适用——装错架构的
    包比不更新糟得多。
    """
    try:
        shaped = normalize_manifest(manifest)
        if shaped["platform"] != str(platform).strip().lower():
            return False
        if shaped["arch"] != str(arch).strip().lower():
            return False
        return parse_version(current_version) < parse_version(shaped["version"])
    except ManifestError:
        return False


def update_is_mandatory(manifest: Mapping[str, Any], current_version: str) -> bool:
    """是否必须强制阻断业务（U4 全屏遮罩的判定入口）。

    两个来源任一成立即强制：清单策略写死 required，或当前版本低于最低可用版本。
    后者的存在是为了能对**已经发出去的旧版本**追加强制，而不必重发一版。
    """
    shaped = normalize_manifest(manifest)
    if shaped["policy"] == POLICY_REQUIRED:
        return True
    return not version_at_least(current_version, shaped["min_supported_version"])


def public_key_fingerprint(public_key_b64: Any) -> Optional[str]:
    """公钥指纹，用于在后台和日志里确认「网关配的公钥和客户端内置的是同一把」。"""
    import hashlib

    try:
        raw = _decode_key(str(public_key_b64), 32)
    except (ValueError, TypeError):
        return None
    return hashlib.sha256(raw).hexdigest()[:16]


# ==================== 共享区 结束 ====================


def sign_manifest(manifest: Mapping[str, Any], private_key_b64: str) -> str:
    """离线签名工具用。**服务端不会调用它**——网关上没有私钥。"""
    key = Ed25519PrivateKey.from_private_bytes(_decode_key(private_key_b64, 32))
    return base64.b64encode(key.sign(canonical_manifest_bytes(manifest))).decode("ascii")
