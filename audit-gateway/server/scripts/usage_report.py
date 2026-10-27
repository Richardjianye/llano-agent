#!/usr/bin/env python3
"""用量报告 —— 放量前后回答「撑不撑得住、哪个模型在掉链子、要花多少钱」

## 为什么不用后台的用量页

后台那页是给日常看的：请求数、token、费用、5 分钟桶。放量决策要的是另外三样，
那里都没有：

1. **并发**。不是「每小时多少次请求」，是「任一瞬间有几路在途」。这台机器扛不
   扛得住只跟后者有关，而它要用 `created_at - latency_ms` 把每次请求还原成一段
   区间、再做扫描线才能算出来。
2. **按模型的成功率与失败原因**。后台混在一起看，一个模型烂掉会被其他模型的量
   稀释掉 —— 2026-09-11 豆包挪到方舟直连后每次调用都 404，就是这样被盖住的。
3. **按座席数外推**。当前 N 人的实测 → 目标 M 人要多少并发、多少钱、多少磁盘。

## 只读，且只碰 usage_records

不读 `conversation_logs`，因此**看不到任何对话正文**。要的字段全是计量维度：
时间、耗时、token、模型、状态。跑这个脚本不构成一次「查阅」，也就不需要进
`audit_access_logs` —— 那张表记的是有人看了谁的内容。

## 用法

    # 最近两天
    python scripts/usage_report.py --dsn "mysql://user:***@host:port/db" --days 2

    # 指定窗口，并按 300 座席外推
    python scripts/usage_report.py --since 2026-09-16 --until 2026-09-18 --seats 300

DSN 也可以从 `USAGE_REPORT_DSN` 或 `MIGRATE_DSN` 环境变量读，避免出现在命令行
历史和进程列表里。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlsplit

# 一次最多拉多少行。两天 58 人大约几千行；真拉到上限说明窗口开太大了，
# 这时候宁可报错也不要给出一份「只统计了前 20 万行」的报告 —— 那种报告的错
# 法是安静的。
ROW_CAP = 200_000

# 外推用的默认参数。都是可以被现实推翻的假设，所以每一个都打印出来。
DEFAULT_DAU_RATIO = 0.40      # 座席里每天真正用的比例
DEFAULT_PEAK_SHARE = 0.25     # 全天请求落在峰值那一小时的比例
BURST_FACTOR = 3.5            # 泊松到达 + 长尾服务时间下，瞬时峰值 / 均值


def parse_dsn(dsn: str) -> dict:
    """解析连接串。照抄 migrate.py：密码里的 @ / : / % 都会被 URL 转义，
    手搓正则遇到含 @ 的密码会切错，而报出来的是「Access denied」。"""
    parts = urlsplit(dsn)
    if not parts.hostname:
        raise SystemExit(f"DSN 解析不出主机名：{dsn.split('@')[-1]}")
    return {
        "host": parts.hostname,
        "port": int(parts.port or 3306),
        "user": unquote(parts.username or "root"),
        "password": unquote(parts.password or ""),
        "database": unquote((parts.path or "/").lstrip("/")),
    }


def pct(sorted_values: list, p: float):
    if not sorted_values:
        return 0
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p))]


def peak_concurrency(rows: list[dict]) -> tuple[int, datetime | None, dict]:
    """把每次请求还原成 [created_at - latency, created_at] 区间，扫描线求峰值。

    `created_at` 记的是**结束**时刻（services/usage.py 在 finally 里写），
    `latency_ms` 是端到端耗时，两者相减就是开始时刻。这是我们能拿到的最接近
    「在途」的东西 —— 它不含客户端排队，但含整个上游等待，而后者正是占住连接
    和内存的那一段。
    """
    events: list[tuple[datetime, int]] = []
    for r in rows:
        end = r["created_at"]
        start = end - timedelta(milliseconds=int(r["latency_ms"] or 0))
        events.append((start, 1))
        events.append((end, -1))
    events.sort()

    cur = peak = 0
    peak_at = None
    by_hour: dict[str, int] = defaultdict(int)
    for t, delta in events:
        cur += delta
        if cur > peak:
            peak, peak_at = cur, t
        key = t.strftime("%Y-%m-%d %H")
        if cur > by_hour[key]:
            by_hour[key] = cur
    return peak, peak_at, dict(by_hour)


def fetch(conn, since: date, until: date) -> list[dict]:
    cols = ("created_at", "latency_ms", "user_uid", "dept_id", "model_name", "status",
            "error_code", "stream", "aborted", "call_kind", "prompt_tokens",
            "completion_tokens", "cached_prompt_tokens", "cost")
    sql = (
        f"SELECT {', '.join(cols)} FROM usage_records "
        "WHERE stat_date >= %s AND stat_date < %s "
        f"ORDER BY created_at LIMIT {ROW_CAP + 1}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (since.isoformat(), until.isoformat()))
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if len(rows) > ROW_CAP:
        raise SystemExit(
            f"窗口内超过 {ROW_CAP:,} 行。把 --days 调小分段跑 —— 截断一份报告"
            "比没有报告更危险，因为看不出它被截断过。"
        )
    return rows


def report(rows: list[dict], since: date, until: date, seats: int,
           dau_ratio: float, peak_share: float) -> None:
    if not rows:
        print(f"窗口 {since} ~ {until} 内没有任何调用记录。")
        return

    days = max(1, (until - since).days)
    users = {r["user_uid"] for r in rows}
    user_days = len({(r["created_at"].date(), r["user_uid"]) for r in rows})
    ok = sum(1 for r in rows if r["status"] == "success")

    print("=" * 74)
    print(f"用量报告  {since} ~ {until}（{days} 天）")
    print("=" * 74)
    print(f"请求总数      {len(rows):,}   成功 {ok:,}（{100*ok/len(rows):.1f}%）")
    print(f"活跃用户      {len(users)} 人，{user_days} 人日")
    print(f"人均          {len(rows)/max(1,user_days):.1f} 次/人日")
    print(f"流式占比      {100*sum(1 for r in rows if r['stream'])/len(rows):.1f}%"
          f"   客户端中断 {sum(1 for r in rows if r['aborted'])} 次")

    # ---- 延迟 ---------------------------------------------------------
    lat = sorted(int(r["latency_ms"] or 0) for r in rows)
    mean_lat = sum(lat) / len(lat)
    print()
    print("耗时(ms)      P50 %-7d P90 %-7d P95 %-7d P99 %-7d max %-8d 均值 %.0f"
          % (pct(lat, .5), pct(lat, .9), pct(lat, .95), pct(lat, .99), lat[-1], mean_lat))

    # ---- token 与缓存 -------------------------------------------------
    pt = sum(int(r["prompt_tokens"] or 0) for r in rows)
    ct = sum(int(r["completion_tokens"] or 0) for r in rows)
    cp = sum(int(r["cached_prompt_tokens"] or 0) for r in rows)
    ptl = sorted(int(r["prompt_tokens"] or 0) for r in rows)
    print("prompt tokens 均 %-8.0f 中位 %-8d P95 %-8d max %d"
          % (pt / len(rows), pct(ptl, .5), pct(ptl, .95), ptl[-1]))
    print("输出 tokens   均 %.0f    缓存命中 %.1f%%（命中率掉下来费用会成倍涨）"
          % (ct / len(rows), 100 * cp / pt if pt else 0))

    # ---- 成本 ---------------------------------------------------------
    cost = sum(float(r["cost"] or 0) for r in rows)
    print("费用          %.2f 元  →  每次 %.4f 元，每百万 token %.2f 元"
          % (cost, cost / len(rows), cost / ((pt + ct) / 1e6) if pt + ct else 0))

    # ---- 按模型：这一段是给「哪个模型在掉链子」看的 --------------------
    print()
    print("按模型")
    print("  %-30s %7s %7s %8s  %s" % ("模型", "请求", "失败", "成功率", "失败原因"))
    agg: dict = defaultdict(lambda: [0, 0, Counter(), []])
    for r in rows:
        a = agg[r["model_name"] or "(未记录)"]
        a[0] += 1
        a[3].append(int(r["latency_ms"] or 0))
        if r["status"] != "success":
            a[1] += 1
            a[2][r["error_code"] or "(无 error_code)"] += 1
    for name, (n, f, errs, lats) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        flag = "  ⚠️" if n >= 20 and f / n > 0.05 else ""
        print("  %-30s %7d %7d %7.1f%%  %s%s"
              % (name[:30], n, f, 100 * (n - f) / n,
                 ", ".join(f"{k}×{v}" for k, v in errs.most_common(4)) or "-", flag))
    print("  （⚠️ = 样本 ≥20 且失败率 >5%。失败率高不一定是模型本身，先看 error_code：")
    print("   upstream_404 多半是地址/路由配错，upstream_400 多半是请求体或参数不合规）")

    # ---- 按天 ---------------------------------------------------------
    print()
    print("按天        请求   活跃用户   人均    成功率")
    by_day: dict = defaultdict(list)
    for r in rows:
        by_day[r["created_at"].date()].append(r)
    for d in sorted(by_day):
        rs = by_day[d]
        u = len({x["user_uid"] for x in rs})
        s = sum(1 for x in rs if x["status"] == "success")
        print("  %s %6d %8d %7.1f %8.1f%%" % (d, len(rs), u, len(rs) / u, 100 * s / len(rs)))

    # ---- 并发：真正决定「撑不撑得住」的那个数 --------------------------
    peak, peak_at, by_hour = peak_concurrency(rows)
    req_by_hour = Counter(r["created_at"].strftime("%Y-%m-%d %H") for r in rows)
    busiest, busiest_n = req_by_hour.most_common(1)[0]
    # 峰值小时占比要拿**那一天自己的**总量做分母。用全窗口日均当分母会算出
    # >100% 的份额（一天很忙、其余天很闲时必然如此），那个数没有意义。
    busiest_day = datetime.strptime(busiest, "%Y-%m-%d %H").date()
    busiest_day_total = len(by_day[busiest_day])
    observed_peak_share = busiest_n / busiest_day_total if busiest_day_total else 0.0
    print()
    print("并发（由 created_at - latency_ms 还原的在途区间）")
    print("  实测峰值      %d 路，发生在 %s" % (peak, peak_at))
    print("  最忙的一小时  %s → %d 次请求（占当天 %d 次的 %.0f%%）"
          % (busiest, busiest_n, busiest_day_total, 100 * observed_peak_share))
    top = sorted(by_hour.items(), key=lambda kv: -kv[1])[:5]
    print("  并发最高的几小时： " + ", ".join(f"{k}→{v}" for k, v in top))

    # ---- 外推 ---------------------------------------------------------
    per_user_day = len(rows) / max(1, user_days)
    dau = seats * dau_ratio
    daily = dau * per_user_day
    peak_hour_reqs = daily * peak_share
    lam = peak_hour_reqs / 3600.0
    concurrent = lam * (mean_lat / 1000.0)
    print()
    print("=" * 74)
    print(f"外推到 {seats} 座席（每个假设都写在这里，不同意就改参数重跑）")
    print("=" * 74)
    print(f"  假设 DAU 比例 {dau_ratio:.0%} → 每天 {dau:.0f} 人")
    print(f"  假设人均沿用实测 {per_user_day:.1f} 次/人日"
          f"（样本 {len(users)} 人；样本越重度，这里越偏高）")
    print(f"  假设峰值小时占全天 {peak_share:.0%}（本窗口实测最忙那天是 {observed_peak_share:.0%}）")
    print()
    print(f"  每天请求      {daily:,.0f} 次")
    print(f"  峰值小时      {peak_hour_reqs:,.0f} 次 → {lam:.2f} 次/秒")
    print(f"  平均在途      {concurrent:.1f} 路（Little's Law：到达率 × 平均耗时 {mean_lat/1000:.1f}s）")
    print(f"  瞬时峰值      约 {concurrent*BURST_FACTOR:.0f} 路（×{BURST_FACTOR} 突发系数）")
    print(f"  每天费用      {daily * (cost/len(rows)):,.0f} 元 → 每月约 {daily*(cost/len(rows))*30:,.0f} 元")
    print(f"  峰值小时 TPM  {peak_hour_reqs * (pt/len(rows)) / 60 / 1e6:.2f} M/分钟"
          f"（按 prompt 均值算；多数厂商的 TPM 按原始 token 计，不减缓存命中）")
    print()
    print("  对照上限：MySQL 连接 = WEB_CONCURRENCY × (db_pool_size + db_max_overflow)")
    print("            生产是 2 × (20+30) = 100，MySQL 默认 max_connections=151。")
    print("            **不要**因为升了内存就把 WEB_CONCURRENCY 调回默认的 4（=200，撞死）。")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=os.environ.get("USAGE_REPORT_DSN")
                   or os.environ.get("MIGRATE_DSN", ""),
                   help="mysql://user:pass@host:port/db（也可用 USAGE_REPORT_DSN）")
    p.add_argument("--days", type=int, default=2, help="往前看几天（默认 2），与 --since 互斥")
    p.add_argument("--since", default="", help="起始日期 YYYY-MM-DD（含）")
    p.add_argument("--until", default="", help="结束日期 YYYY-MM-DD（不含），默认明天")
    p.add_argument("--seats", type=int, default=300, help="外推到多少座席（默认 300）")
    p.add_argument("--dau-ratio", type=float, default=DEFAULT_DAU_RATIO)
    p.add_argument("--peak-share", type=float, default=DEFAULT_PEAK_SHARE)
    args = p.parse_args()

    if not args.dsn:
        sys.stderr.write("缺 --dsn（或环境变量 USAGE_REPORT_DSN / MIGRATE_DSN）\n")
        return 2

    until = (date.fromisoformat(args.until) if args.until
             else date.today() + timedelta(days=1))
    since = date.fromisoformat(args.since) if args.since else until - timedelta(days=args.days)

    conn_args = parse_dsn(args.dsn)
    import pymysql

    print(f"目标 {conn_args['user']}@{conn_args['host']}:{conn_args['port']}"
          f"/{conn_args['database']}（密码 {len(conn_args['password'])} 字符，不打印）\n")
    conn = pymysql.connect(**conn_args, charset="utf8mb4", autocommit=True)
    try:
        rows = fetch(conn, since, until)
    finally:
        conn.close()

    report(rows, since, until, args.seats, args.dau_ratio, args.peak_share)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
