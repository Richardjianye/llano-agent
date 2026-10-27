#!/usr/bin/env python3
"""自有 skill hub 的端到端对账 —— 发布 → 落库 → 搜索 → 取包 → 装得上 → 下架

## 为什么必须有这一条

阶段 11 各段各有单测，但**数据库那一层一条都没有**。publish_skill 里最容易出错的
四条判断全都压在真实的唯一约束上：

  1. 同内容不新增版本（(skill_id, bundle_hash) 唯一）
  2. 版本号并发不冲突（(skill_id, version) 唯一，撞了要重试）
  3. 只有发布者能发新版
  4. 下架之后发不了新版、也搜不到

喂一个假 session 测这四条得到的绿是假的 —— 假 session 不会真的撞唯一键。所以这
一段交给这个脚本，对着一次性 MySQL 容器跑，与 skill_report_chain.py 同一套办法。

## 还验一件单测验不了的事：**发出去的包装得上**

网关侧测了「编码信封解析正确」，客户端侧测了「信封还原正确」，但两边各自 mock 了
对方。真正要回答的是：员工 A 发布的那份，员工 B 装下来之后**逐字节相同**吗。
所以脚本会把取回来的 bundle 与原始目录做逐文件比对，二进制也比。

## 前置

与 skill_report_chain.py 相同：一个跑着的网关 + 一把该网关签发的 API Key +
一个总裁 JWT。用一次性容器最省事（不会碰到任何真实环境）：

    docker network create ag-e2e
    docker run -d --name e2e-mysql --network ag-e2e \\
      -e MYSQL_ROOT_PASSWORD=e2e-throwaway -e MYSQL_DATABASE=audit_gateway mysql:8
    docker run -d --name e2e-redis --network ag-e2e redis:8-alpine
    docker run -d --name e2e-gw --network ag-e2e -p 7902:7899 \\
      -e ENVIRONMENT=dev \\
      -e MYSQL_URL="mysql+aiomysql://root:e2e-throwaway@e2e-mysql:3306/audit_gateway?charset=utf8mb4" \\
      -e REDIS_URL="redis://e2e-redis:6379/0" \\
      -e JWT_SECRET_KEY=... -e JWT_REFRESH_SECRET_KEY=... audit-gateway:test

⚠️ **JWT 光签名对是不够的**：`is_token_revoked` 要求 jti 在 Redis 的
`active_token:` 白名单里。令牌必须走 `token_manager.store_token()` 登记
（这一条 skill_report_chain 踩过，现象是「签名自解自验都过，接口照样回 10002」）。

## 用法

    HERMES_HOME=/tmp/e2e-hub-home HERMES_MANAGED_DIR=/tmp/e2e-hub-managed \\
    LLANO_GATEWAY_API_KEY=sk-llano-... \\
    python scripts/e2e/skill_hub_chain.py --origin http://127.0.0.1:7902 --admin-token <JWT>

HERMES_HOME 必须指到临时目录 —— 脚本会往活动技能树里放东西。

⚠️ 第 6 步「非拥有者不能发新版」需要**第二把** API Key（另一个账号的）。没给
   `--other-api-key` 时这一步会跳过并在结尾明确报出来 —— 静默跳过的话，一份
   「全绿」的报告会让人以为这条最容易写错的判断被验过了。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.reporter import Reporter  # noqa: E402

SKILL_MD = (
    "---\nname: e2e-hub-demo\ndisplay_name: 技能发布演示\ndescription: E2E 用的演示技能。\n---\n\n"
    "# 演示\n\n## 步骤\n1. 什么都不做\n"
)
# 故意放一段非 UTF-8 的字节：整条链路上唯一会走 base64 分支的东西。
# 只用文本文件测的话，信封那一半永远跑不到。
BINARY_BLOB = bytes(range(256)) * 4


def _call(origin: str, path: str, token: str, *, method: str = "GET", body=None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{origin.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def run(origin: str, api_key: str, admin_token: str, other_api_key: str = "") -> Reporter:
    from hermes_constants import get_hermes_home
    from tools.skill_publish import build_payload

    r = Reporter()
    home = get_hermes_home()
    if home == Path.home() / ".hermes" and not os.environ.get("HERMES_HOME"):
        sys.exit("拒绝在默认 HERMES_HOME 下运行：会往真实技能库里塞东西。请设 HERMES_HOME。")

    skill = home / "skills" / "e2e-hub-demo"
    (skill / "assets").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (skill / "assets" / "blob.bin").write_bytes(BINARY_BLOB)

    r.section("1. 首次发布")
    payload, skipped = build_payload(skill, slug="e2e-hub-demo", changelog="首版")
    status, body = _call(origin, "/v1/skill-hub/skills", api_key, method="POST", body=payload)
    r.check(status == 201, f"首发应回 201（实际 {status} {body}）")
    r.check(body.get("version") == 1, f"首发应是 v1（实际 {body.get('version')}）")
    first_hash = body.get("bundle_hash")

    r.section("2. 同内容再发一次 —— 不该新增版本")
    status, body = _call(origin, "/v1/skill-hub/skills", api_key, method="POST", body=payload)
    r.check(status == 200, f"内容没变应回 200 而不是 201（实际 {status}）")
    r.check(body.get("created") is False, "created 应为 False")
    r.check(body.get("version") == 1, f"版本号不该涨（实际 {body.get('version')}）")

    r.section("3. 换个发法发同一份内容 —— 指纹算的是解码后的字节")
    # SKILL.md 改用 base64 发。内容一个字节都没变，所以仍然不该新增版本。
    alt = json.loads(json.dumps(payload))
    for f in alt["files"]:
        if f["path"] == "SKILL.md":
            f["encoding"] = "base64"
            f["content"] = base64.b64encode(SKILL_MD.encode("utf-8")).decode("ascii")
    status, body = _call(origin, "/v1/skill-hub/skills", api_key, method="POST", body=alt)
    r.check(body.get("version") == 1, "换编码不该凭空多出一版")
    r.check(body.get("bundle_hash") == first_hash, "换编码不该改变内容指纹")

    r.section("4. 改内容再发 —— 应该是 v2")
    (skill / "SKILL.md").write_text(SKILL_MD + "\n2. 还是什么都不做\n", encoding="utf-8")
    payload2, _ = build_payload(skill, slug="e2e-hub-demo", changelog="加了一步")
    status, body = _call(origin, "/v1/skill-hub/skills", api_key, method="POST", body=payload2)
    r.check(status == 201 and body.get("version") == 2, f"改内容应是 v2（实际 {status} {body}）")

    r.section("5. 搜得到，且取回来的包逐字节相同")
    status, body = _call(origin, "/v1/skill-hub/skills?q=e2e-hub", api_key)
    slugs = [s["slug"] for s in body.get("skills", [])]
    r.check("e2e-hub-demo" in slugs, f"搜索应找到（实际 {slugs}）")

    status, bundle = _call(origin, "/v1/skill-hub/skills/e2e-hub-demo/bundle", api_key)
    r.check(status == 200, f"取包应成功（实际 {status}）")

    from tools.llano_skill_hub import decode_files

    got = decode_files(bundle.get("files", []))
    want = {
        "SKILL.md": (skill / "SKILL.md").read_text(encoding="utf-8"),
        "assets/blob.bin": BINARY_BLOB,
    }
    r.check(set(got) == set(want), f"文件清单应一致（实际 {sorted(got)}）")
    r.check(got.get("SKILL.md") == want["SKILL.md"], "文本文件应逐字符相同")
    # 这一条是整个脚本的核心：两边各自 mock 对方时，base64 这一半永远没被端到端验过。
    r.check(got.get("assets/blob.bin") == BINARY_BLOB, "二进制应逐字节相同")
    r.check(isinstance(got.get("assets/blob.bin"), bytes), "二进制必须还原成 bytes 而不是 str")

    r.section("6. 非拥有者不能发新版")
    if other_api_key:
        status, body = _call(origin, "/v1/skill-hub/skills", other_api_key, method="POST", body=payload2)
        r.check(status == 403, f"别人发新版应回 403（实际 {status} {body}）")
    else:
        # warn 而不是 info：静默跳过的话，一份「全部通过」的报告会让人以为
        # 这条最容易写错的判断被验过了。
        r.warn("跳过「非拥有者不能发新版」——没给 --other-api-key，本轮没有验过这一条")

    r.section("7. 后台看得到统计")
    status, body = _call(origin, "/admin/skill-hub/stats", admin_token)
    r.check(body.get("code") == 0, f"统计接口应成功（实际 {body}）")
    pubs = body.get("data", {}).get("by_publisher", [])
    r.check(bool(pubs), "按人统计不该为空")
    r.check(
        any(p["version_count"] >= 2 for p in pubs),
        f"发布者应记到 2 次发布（实际 {pubs}）",
    )

    r.section("8. 下架之后客户端彻底看不到")
    status, body = _call(origin, "/admin/skill-hub/list", admin_token)
    row = next(s for s in body["data"]["items"] if s["slug"] == "e2e-hub-demo")
    status, body = _call(
        origin, "/admin/skill-hub/delist", admin_token,
        method="POST", body={"skill_id": row["id"], "reason": "E2E 验证"},
    )
    r.check(body.get("code") == 0, f"下架应成功（实际 {body}）")

    status, body = _call(origin, "/v1/skill-hub/skills?q=e2e-hub", api_key)
    r.check(
        "e2e-hub-demo" not in [s["slug"] for s in body.get("skills", [])],
        "下架后不该还搜得到",
    )
    status, _b = _call(origin, "/v1/skill-hub/skills/e2e-hub-demo/bundle", api_key)
    r.check(status == 404, f"下架后取包应 404（实际 {status}）")
    status, body = _call(origin, "/v1/skill-hub/skills", api_key, method="POST", body=payload2)
    r.check(status == 403, f"下架后发新版应 403（实际 {status} {body}）")

    r.section("9. 恢复上架")
    status, body = _call(
        origin, "/admin/skill-hub/relist", admin_token, method="POST", body={"skill_id": row["id"]}
    )
    r.check(body.get("code") == 0, "恢复应成功")
    status, body = _call(origin, "/v1/skill-hub/skills/e2e-hub-demo", api_key)
    r.check(status == 200, f"恢复后应又能 inspect（实际 {status}）")

    return r


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", default="http://127.0.0.1:7902")
    parser.add_argument("--admin-token", required=True, help="总裁 JWT（必须已 store_token 登记）")
    parser.add_argument("--api-key", default=os.environ.get("LLANO_GATEWAY_API_KEY", ""))
    parser.add_argument(
        "--other-api-key", default="",
        help="另一个账号的 Key。不给的话第 6 步（非拥有者不能发新版）会跳过",
    )
    args = parser.parse_args()

    if not args.api_key:
        sys.stderr.write("缺 --api-key（或环境变量 LLANO_GATEWAY_API_KEY）\n")
        return 2

    reporter = run(args.origin, args.api_key, args.admin_token, args.other_api_key)
    print()
    for w in reporter.warnings:
        print(f"⚠️  {w}")
    if reporter.ok:
        print(f"全部通过（{len(reporter.checks)} 项）")
        return 0
    print(f"失败 {len(reporter.failures)} / {len(reporter.checks)} 项：")
    for f in reporter.failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
