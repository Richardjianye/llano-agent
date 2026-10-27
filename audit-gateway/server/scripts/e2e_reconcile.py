"""只对账，不发请求 —— 供客户端驱动的链路测试跨语言调用（阶段 4 · CP1）。

## 它和 `e2e_audit_check.py` 的分工

| | 谁发请求 | 花不花钱 | 用途 |
| - | -------- | -------- | ---- |
| `e2e_audit_check.py` | 脚本自己发 HTTP | 会（真实上游） | 网关侧端到端 |
| **本脚本** | **谁都不发** | **不花** | 别人发完之后回头核对 |

阶段 4 第 1 条要的完整链路是 Playwright 开真实 Electron 走完登录 → 切模型 →
对话，那些请求由**客户端**发出。发完之后要核对的东西（发几次记几笔、中断不算失败、
明细不许带聊天内容……）和网关侧脚本要核对的完全是同一套，所以口径统一放在
`scripts/e2e/reconcile.py`，两边共用。

Playwright 是 TypeScript，import 不了 Python 模块，所以这里包一层 CLI：
出 JSON（`--json`）给程序解析，出人类可读排版给人看。

## 用法

    # 客户端跑完之后，核对这段时间新增了几笔
    python scripts/e2e_reconcile.py --user admin --password '...' \\
        --expect-requests 6 --baseline 128 --json

`--baseline` 是客户端动手**之前**的请求总数；不给的话就只核对「有没有记录」，
核对不了「这次记了几笔」—— 而漏记正是最危险的那类 bug（报表上少一笔和正常
长得一模一样）。基线可以先用 `--print-baseline` 取。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from e2e.gateway import Gateway  # noqa: E402
from e2e.reconcile import (  # noqa: E402
    check_access_logs,
    check_attachment_download,
    check_audit,
    check_export,
    check_usage,
)
from e2e.reporter import FAIL, PASS, Reporter  # noqa: E402


async def _run(args) -> int:
    reporter = Reporter(quiet=args.json)
    gw = Gateway(args.base_url, reporter)

    try:
        await gw.login(args.user, args.password)

        if args.print_baseline:
            total = (await gw.admin_get("/admin/usage/summary"))["total"]["requests"]
            print(json.dumps({"baseline": total}) if args.json else total)
            return 0

        detail = await check_audit(gw, reporter, args.sentinel)
        if detail:
            await check_attachment_download(gw, reporter, detail)
        await check_access_logs(gw, reporter, detail)

        # expect_requests 缺省为 0：check_usage 里是 `actual >= expected`，
        # 传 0 就退化成「只要有记录就行」。这不是巧合而是刻意的 ——
        # 调用方不知道客户端到底发了几次时，宁可少断言，也不要瞎猜一个数
        # 然后报一个假失败
        await check_usage(
            gw,
            reporter,
            expected_requests=args.expect_requests,
            baseline=args.baseline,
        )
        await check_export(gw, reporter)

    except RuntimeError as exc:
        reporter.fail(str(exc))
        if not args.json:
            print(f"\n{FAIL} 接口调用失败：{exc}")
    finally:
        await gw.close()

    if args.json:
        print(json.dumps(reporter.to_dict(), ensure_ascii=False))
    else:
        reporter.section("结论")
        if reporter.ok:
            print(f"{PASS} 对账通过")
        else:
            print(f"{FAIL} {len(reporter.failures)} 项未通过：")
            for item in reporter.failures:
                print(f"   · {item}")

    return 0 if reporter.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读对账：核对审计 / 附件 / 查阅日志 / 用量四条链路"
    )
    parser.add_argument("--base-url", default="http://localhost:7899", help="网关地址")
    parser.add_argument("--user", required=True, help="登录用户名（中文花名）")
    parser.add_argument("--password", required=True, help="登录密码")
    parser.add_argument(
        "--expect-requests",
        type=int,
        default=0,
        help="本轮预期记账笔数；0 表示只核对「有记录」不核对「几笔」",
    )
    parser.add_argument(
        "--baseline", type=int, default=0, help="动手前的请求总数（用 --print-baseline 取）"
    )
    parser.add_argument("--sentinel", default=None, help="双通道审计原文哨兵关键词")
    parser.add_argument(
        "--print-baseline",
        action="store_true",
        help="只打印当前请求总数然后退出，供调用方在动手前取基线",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
