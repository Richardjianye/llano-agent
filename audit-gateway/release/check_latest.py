#!/usr/bin/env python3
"""发版收尾检查：网关发布的版本、固定下载入口、分发机上的文件，三者一致吗？

背景：客户端自动更新读的是网关 `/v1/client-update`，而新同事装机用的是分发机上的
固定入口 `http://192.0.2.130/releases/latest/{win,mac}`（IIS web.config 里的 302）。
两者是两套东西，改了一个常忘另一个 —— 0.18.3 就是网关 09-24 发布了、固定入口
到 09-29 还指着 0.18.2。这个脚本是 RELEASE-CHECKLIST.md 第五节的最后一步，
不通过不算发完。

只读：GET 网关清单、不跟随跳转地读 latest 的 Location、HEAD 安装包。不改任何东西。

用法：

    python3 audit-gateway/release/check_latest.py
    python3 audit-gateway/release/check_latest.py --expect-version 0.18.4

退出码：0 一致；1 不一致（逐条说明怎么修）；2 网络或参数问题。
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_GATEWAY = "https://gateway.example.invalid"
DEFAULT_DIST = "http://192.0.2.130"

# (网关里的 platform, arch, 固定入口的名字)
PLATFORMS = (("win32", "x64", "win"), ("darwin", "arm64", "mac"))


@dataclass
class Published:
    version: str
    url: str
    sha256: str
    size: int


def _ssl_context() -> ssl.SSLContext:
    # python.org 装的 macOS Python 不带系统根证书，有 certifi 就用它
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    # 分发机在内网，本机常开着 Clash 之类的代理；绕开代理才能直连 130
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_ssl_context())
    )


def published_manifest(gateway: str, platform: str, arch: str) -> Published | None:
    query = urllib.parse.urlencode({"platform": platform, "arch": arch, "version": "0.0.0"})
    with urllib.request.urlopen(f"{gateway}/v1/client-update?{query}", timeout=20) as resp:
        body = json.load(resp)
    manifest = ((body or {}).get("update") or {}).get("manifest")
    if not manifest:
        return None
    return Published(
        version=str(manifest["version"]),
        url=str(manifest["artifact_url"]),
        sha256=str(manifest["artifact_sha256"]),
        size=int(manifest["artifact_size"]),
    )


def _request(method: str, url: str) -> http.client.HTTPResponse:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.netloc, timeout=20, context=_ssl_context()
        )
    else:
        conn = http.client.HTTPConnection(parsed.netloc, timeout=20)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    conn.request(method, path)
    return conn.getresponse()


def latest_target(dist: str, name: str) -> tuple[int, str]:
    """固定入口跳到哪儿。不跟随跳转，返回 (状态码, Location)。"""
    resp = _request("GET", f"{dist}/releases/latest/{name}")
    location = resp.getheader("Location") or ""
    resp.read(0)
    return resp.status, urllib.parse.urljoin(f"{dist}/releases/latest/{name}", location)


def artifact_head(url: str) -> tuple[int, int | None]:
    resp = _request("HEAD", url)
    length = resp.getheader("Content-Length")
    return resp.status, int(length) if length and length.isdigit() else None


def check(gateway: str, dist: str, expect_version: str | None) -> list[str]:
    problems: list[str] = []

    for platform, arch, name in PLATFORMS:
        label = f"{platform}-{arch}"
        published = published_manifest(gateway, platform, arch)
        if published is None:
            problems.append(f"[{label}] 网关没有下发任何版本")
            continue

        print(f"[{label}] 网关发布：{published.version}  {published.url}")

        if expect_version and published.version != expect_version:
            problems.append(
                f"[{label}] 网关下发的是 {published.version}，不是 {expect_version}："
                "后台「桌面端版本」页还没录入，或录错了平台"
            )

        status, target = latest_target(dist, name)
        print(f"[{label}] 固定入口：/releases/latest/{name} → HTTP {status} {target or '（无跳转）'}")
        if status not in (301, 302, 303, 307, 308):
            problems.append(f"[{label}] /releases/latest/{name} 没有跳转（HTTP {status}）")
        elif target != published.url:
            problems.append(
                f"[{label}] 固定入口还指着 {target}，网关已发布 {published.url}。"
                f"在 130 上执行 switch_latest.ps1 -Version {published.version}（见 RELEASE-CHECKLIST.md 第五节）"
            )

        head_status, length = artifact_head(published.url)
        if head_status != 200:
            problems.append(f"[{label}] 安装包 HEAD 返回 HTTP {head_status}：{published.url}")
        elif length != published.size:
            problems.append(
                f"[{label}] 安装包大小 {length} 与清单声明的 {published.size} 不一致 —— 分发机上的文件被换过？"
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--dist", default=DEFAULT_DIST, help="分发机 origin")
    parser.add_argument("--expect-version", default=None, help="刚发布的版本号，比如 0.18.4")
    args = parser.parse_args(argv)

    # urllib 的全局 opener 也绕开代理：网关走公网没关系，分发机必须直连
    urllib.request.install_opener(_no_proxy_opener())

    try:
        problems = check(args.gateway.rstrip("/"), args.dist.rstrip("/"), args.expect_version)
    except (OSError, ValueError, KeyError) as exc:
        print(f"检查没做完：{exc}", file=sys.stderr)
        return 2

    if problems:
        print("\n不一致：", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print("\n一致：网关、固定入口、分发机文件指向同一个版本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
