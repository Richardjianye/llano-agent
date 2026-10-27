"""客户端侧的更新清单验签 —— 网关那份实现的**只读镜像**

对应文件：audit-gateway/server/services/release_manifest.py

这里刻意没有 `sign_manifest`。客户端手里只有公钥，它能判断一份清单是不是我们
发的，但签不出新的——这正是签名要保证的事，别给它签名能力。

## 为什么是两份代码而不是一份

网关和客户端是两个独立部署单元：网关跑在公司服务器上，这个文件跟着桌面端装到
每个人的机器里。没有第三个地方能放一份被两边同时 import 的共享库。

代价是 `canonical_manifest_bytes()` 必须与网关那份**逐字节一致**。差一个空格
的后果不是某台机器出错，而是**全量客户端同时验签失败、集体更新不上**，且现象
是「签名验证不通过」，第一反应会去查密钥而不是查序列化。

挡住这件事的唯一机制是固定向量：`audit-gateway/release/manifest-vectors.json`
被两边的测试各读一遍。改动这个文件里 `# ==== 共享区 ====` 之内的任何一行，
都必须同步改网关那份，然后两边测试都跑。

## 公钥从哪来

编译进构建物，**不从网关取**。从网关取的公钥等于没有签名：能改清单的人自然
也能改公钥。见 _BUILTIN_UPDATE_PUBLIC_KEY。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import \
    Ed25519PublicKey

# ==================== 共享区 开始 ====================
# 以下内容必须与 audit-gateway/server/services/release_manifest.py 逐字一致

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


class UpdateSignatureError(RuntimeError):
    """清单没有通过验签，或本构建根本没有可用的更新公钥。"""


# 打包时由构建脚本写死成本次发布使用的公钥。
#
# 仓库里留空是有意的：源码树里没有生产公钥，改这一行等于换一套信任根，
# 应当是一次显式的、能在 diff 里看见的操作。
#
# 为什么不从网关下发：能改清单的人自然也能改公钥，那样签名就退化成了
# 「网关说这份清单是对的」——而签名要证明的恰恰是「即使网关被控制，客户端也
# 能识别出这不是我们发的」。
_BUILTIN_UPDATE_PUBLIC_KEY = ""

_PUBLIC_KEY_ENV = "HERMES_UPDATE_PUBLIC_KEY"


def builtin_public_key() -> str:
    """本构建信任的更新公钥。

    正式构建里 _BUILTIN_UPDATE_PUBLIC_KEY 一定非空，此时环境变量被忽略——
    否则任何能设环境变量的人都能替换信任根，签名就白做了。环境变量只在源码
    树里（常量为空）起作用，供开发与测试用。
    """
    import os

    if _BUILTIN_UPDATE_PUBLIC_KEY:
        return _BUILTIN_UPDATE_PUBLIC_KEY
    return (os.environ.get(_PUBLIC_KEY_ENV) or "").strip()


def require_trusted_manifest(
    manifest: Mapping[str, Any],
    signature: Any,
    *,
    public_key: Optional[str] = None,
) -> Dict[str, Any]:
    """验签通过就返回归一化后的清单，否则抛。

    没配公钥也抛，不是「跳过校验」：一个不知道该信任谁的客户端，唯一安全的
    行为是拒绝安装任何东西。
    """
    key = public_key if public_key is not None else builtin_public_key()
    if not key:
        raise UpdateSignatureError("本构建没有内置更新公钥，拒绝安装任何更新")
    if not verify_manifest(manifest, signature, key):
        raise UpdateSignatureError("更新清单验签失败，拒绝安装")
    return normalize_manifest(manifest)
