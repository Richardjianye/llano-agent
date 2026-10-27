#!/usr/bin/env python3
"""离线签名工具 —— **只在签名机上运行，不要部署到网关**

用法：

    # 一次性：生成签名密钥对（长期使用，不随发版更换）
    python3 sign_release.py keygen --out ~/llano-signing/update-key

    # 每次发版：对构建物算哈希、生成清单、签名
    python3 sign_release.py sign \\
        --key ~/llano-signing/update-key.private \\
        --artifact ./dist/LLANO Agent-1.4.0-arm64.dmg \\
        --url https://gw.company/downloads/llano-1.4.0-arm64.dmg \\
        --version 1.4.0 --platform darwin --arch arm64 \\
        --policy required --min-supported 1.2.0 \\
        --notes-file ./RELEASE_NOTES.md \\
        --out ./release-1.4.0-darwin-arm64.json

    # 把 --out 产出的 JSON 整个贴进后台「发布新版本」，网关会用公钥验一遍才入库

密钥保管：

- `*.private` 是 32 字节种子的 base64。**它离开签名机的那一刻，签名就不再
  证明任何事情**——拿到它的人可以签出一份客户端会照单全收的恶意更新。
  不进仓库、不进网关、不进聊天工具，与 `~/Desktop/llano-agent-keys` 同等对待。
- `*.public` 编译进客户端、配置到网关（`UPDATE_PUBLIC_KEY`）。
- 密钥对是长期的，**不随发版更换**。只有在轮换或私钥泄露时才换，代价是必须
  推一次全量安装包——旧客户端内置的是旧公钥，新签名它验不过。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone

# 复用网关那份实现，避免第三份规范序列化
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

from services.release_manifest import (  # noqa: E402
    ManifestError, normalize_manifest, public_key_fingerprint, sign_manifest,
    verify_manifest)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_keygen(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey

    private_path = f"{args.out}.private"
    public_path = f"{args.out}.public"
    for path in (private_path, public_path):
        if os.path.exists(path) and not args.force:
            print(f"拒绝覆盖已存在的 {path}（确认要换密钥对再加 --force）", file=sys.stderr)
            return 2

    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes_raw()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")

    os.makedirs(os.path.dirname(os.path.abspath(private_path)) or ".", exist_ok=True)
    # 先建成 0600 再写，避免明文私钥有一瞬间是默认权限
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(base64.b64encode(seed).decode("ascii") + "\n")
    with open(public_path, "w", encoding="utf-8") as fh:
        fh.write(public + "\n")

    print(f"私钥 → {private_path}（权限 0600，不要离开这台机器）")
    print(f"公钥 → {public_path}")
    print(f"公钥       {public}")
    print(f"公钥指纹   {public_key_fingerprint(public)}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.artifact):
        print(f"构建物不存在：{args.artifact}", file=sys.stderr)
        return 2

    notes = ""
    if args.notes_file:
        with open(args.notes_file, encoding="utf-8") as fh:
            notes = fh.read()
    elif args.notes:
        notes = args.notes

    released_at = args.released_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_manifest = {
        "version": args.version,
        "platform": args.platform,
        "arch": args.arch,
        "policy": args.policy,
        "min_supported_version": args.min_supported,
        "artifact_url": args.url,
        "artifact_sha256": _sha256_file(args.artifact),
        "artifact_size": os.path.getsize(args.artifact),
        "release_notes": notes,
        "released_at": released_at,
    }

    try:
        manifest = normalize_manifest(raw_manifest)
    except ManifestError as exc:
        print(f"清单不合法：{exc}", file=sys.stderr)
        return 2

    with open(args.key, encoding="utf-8") as fh:
        private_key = fh.read().strip()

    signature = sign_manifest(manifest, private_key)

    # 自验一遍：签完就地验不过说明工具本身坏了，别把废签名发出去
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey
    public_key = base64.b64encode(
        Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_key, validate=True)
        ).public_key().public_bytes_raw()
    ).decode("ascii")
    if not verify_manifest(manifest, signature, public_key):
        print("自验失败：签名工具异常，请勿发布", file=sys.stderr)
        return 3

    payload = {"manifest": manifest, "signature": signature}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已签名 → {args.out}")
    else:
        sys.stdout.write(text)

    print(f"SHA-256    {manifest['artifact_sha256']}", file=sys.stderr)
    print(f"公钥指纹   {public_key_fingerprint(public_key)}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLANO Agent客户端更新清单离线签名工具")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="生成长期签名密钥对")
    keygen.add_argument("--out", required=True, help="输出前缀，会生成 <out>.private 和 <out>.public")
    keygen.add_argument("--force", action="store_true", help="覆盖已有密钥（等于作废所有已发客户端）")
    keygen.set_defaults(func=cmd_keygen)

    sign = sub.add_parser("sign", help="对一个构建物生成已签名清单")
    sign.add_argument("--key", required=True, help="私钥文件路径")
    sign.add_argument("--artifact", required=True, help="安装包路径，哈希和体积从这里算")
    sign.add_argument("--url", required=True, help="客户端下载地址")
    sign.add_argument("--version", required=True)
    sign.add_argument("--platform", required=True, choices=("darwin", "win32"))
    sign.add_argument("--arch", required=True, choices=("x64", "arm64"))
    sign.add_argument("--policy", required=True, choices=("optional", "required"))
    sign.add_argument("--min-supported", required=True, help="低于该版本的客户端必须强制更新")
    sign.add_argument("--notes", default="", help="发行说明文本")
    sign.add_argument("--notes-file", default="", help="发行说明文件，优先于 --notes")
    sign.add_argument("--released-at", default="", help="UTC，形如 2026-08-24T02:30:00Z，默认取当前时间")
    sign.add_argument("--out", default="", help="输出文件，缺省打到 stdout")
    sign.set_defaults(func=cmd_sign)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
