#!/usr/bin/env python3
"""迁移执行器 —— 发布流程的第一步：先对齐库结构，再上新代码

## 为什么必须先跑库再上代码

`warn_schema_drift` 在 prod 是**拒绝启动**的。新代码里模型多了一列、库里没有，
网关不是「功能不可用」，是**直接起不来**。所以顺序只能是：迁移 → 部署。

反过来（先部署再迁移）在 Zeabur 上尤其糟：那里没有终端也没有 SSH，服务起不来
之后你**没有任何手段**进去补跑迁移，只能回滚代码。

## 与 `run_migration.py` 那个临时脚本的区别

那个是手动场景用的，把「列已存在」当成已应用来兜底。**做成自动流程就不够了**：
它分不清「上次跑过」和「有人手工加过一个同名但类型不同的列」——后者会被静默
当成成功，而库结构其实是错的。

这里改成三条硬规则：

1. **跟踪表**（`schema_migrations`）：每个文件按文件名记一行，跑过就不再跑。
   不猜、不靠错误码
2. **建议锁**（`GET_LOCK`）：Zeabur 的部署方式是「起新容器 → 跑一段 → 淘汰旧
   容器」，两个容器会同时在线。没有锁的话两个进程会同时 ALTER 同一张表
3. **失败即退出非零**：半截迁移比不迁移更糟。发布流程据此中止，不要接着上代码

## 用法

    # 看有哪些没跑（不改库）
    python scripts/migrate.py --dsn "mysql://root:***@host:port/db" --dry-run

    # 真跑
    python scripts/migrate.py --dsn "..."

DSN 也可以从 `MIGRATE_DSN` 环境变量读，避免出现在命令行历史与进程列表里。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from urllib.parse import unquote, urlsplit

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "migrations"
# 锁名与库名无关：同一个 MySQL 实例上跑多个环境时，各自的库应当各自加锁。
LOCK_NAME_PREFIX = "audit_gateway_migrate"
LOCK_TIMEOUT_SECONDS = 60

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS `schema_migrations` (
  `filename` VARCHAR(255) NOT NULL COMMENT '迁移文件名，即版本号',
  `applied_at` DATETIME NOT NULL COMMENT '执行时间（服务器本地时间，全库统一口径）',
  `statements` INT NOT NULL DEFAULT 0 COMMENT '执行了几条语句',
  PRIMARY KEY (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='已执行的迁移。每个文件按文件名记一行，跑过就不再跑'
"""


def parse_dsn(dsn: str) -> dict:
    """解析连接串。

    **用 urllib 不手搓正则** —— 密码里的 `@` / `:` / `%` 都会被 URL 转义，
    手搓的 `([^@]+)` 遇到含 `@` 的密码直接切错，而报出来的是「Access denied」，
    跟真正的原因隔着一层。2026-08-31 在 108 上踩过一次。
    """
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


def split_statements(sql: str) -> list[str]:
    """按分号切。整行注释先去掉 —— 注释里出现的分号会把语句切碎。

    这些迁移里没有存储过程，所以不需要 DELIMITER 处理。哪天真要写触发器，
    这个函数要先改，别指望它凑合。
    """
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", default=os.environ.get("MIGRATE_DSN", ""),
                        help="mysql://user:pass@host:port/db（也可用 MIGRATE_DSN 环境变量）")
    parser.add_argument("--dry-run", action="store_true", help="只列出待执行的文件，不改库")
    parser.add_argument(
        "--adopt-through", default="",
        help="把截至这个文件（含）的迁移**记为已应用但不执行**，用于接管一个已经"
             "手工跑过迁移的库。传 all 表示全部。这是一次性动作，做错了会让真正"
             "没跑的迁移被永久跳过 —— 先用 --dry-run 看清楚",
    )
    args = parser.parse_args()

    if not args.dsn:
        sys.stderr.write("缺 --dsn（或环境变量 MIGRATE_DSN）\n")
        return 2

    conn_args = parse_dsn(args.dsn)
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("migrations/ 下没有文件")
        return 0

    import pymysql

    # 连接信息里只打印非敏感的部分。
    print(f"目标 {conn_args['user']}@{conn_args['host']}:{conn_args['port']}"
          f"/{conn_args['database']}（密码 {len(conn_args['password'])} 字符，不打印）")

    conn = pymysql.connect(**conn_args, charset="utf8mb4", autocommit=True)
    lock_name = f"{LOCK_NAME_PREFIX}_{conn_args['database']}"
    applied_count = 0

    try:
        with conn.cursor() as cur:
            cur.execute(TRACKING_DDL)

            # Zeabur 的部署方式是「起新容器 → 跑一段 → 淘汰旧容器」，两个容器会
            # 同时在线。没有这把锁，两个进程会同时 ALTER 同一张表。
            cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, LOCK_TIMEOUT_SECONDS))
            got = cur.fetchone()[0]
            if got != 1:
                sys.stderr.write(
                    f"拿不到迁移锁（{LOCK_TIMEOUT_SECONDS}s 超时）——"
                    f"多半是另一个实例正在跑迁移。稍后重试，**不要**强行继续\n"
                )
                return 1

            try:
                cur.execute("SELECT filename FROM schema_migrations")
                done = {row[0] for row in cur.fetchall()}
                pending = [f for f in files if f.name not in done]

                # ---- 接管一个已经手工跑过迁移的库 ----
                #
                # 跟踪表是后加的，而 108 与 Zeabur 的库早就手工跑过一批迁移了。
                # 首次运行会把那些文件当成「待执行」重跑一遍：CREATE TABLE IF
                # NOT EXISTS 无所谓，但 ALTER 会撞 duplicate column 直接失败。
                #
                # 所以要有一个显式的「认账」动作。**必须显式**：自动检测
                # 「这张表已经存在所以大概跑过了」是猜，而猜错的方向是致命的
                # —— 真正没跑的迁移被永久跳过，且没有任何一处会报错。
                if args.adopt_through:
                    cutoff = args.adopt_through.strip()
                    if cutoff.lower() == "all":
                        adopt = [f for f in files if f.name not in done]
                    else:
                        names = [f.name for f in files]
                        if cutoff not in names:
                            sys.stderr.write(f"--adopt-through 指定的文件不存在：{cutoff}\n")
                            return 2
                        limit = names.index(cutoff)
                        adopt = [f for f in files[: limit + 1] if f.name not in done]

                    for f in adopt:
                        cur.execute(
                            "INSERT INTO schema_migrations (filename, applied_at, statements) "
                            "VALUES (%s, NOW(), 0)",
                            (f.name,),
                        )
                        print(f"   认账  {f.name}（记为已应用，未执行）")
                    print(f"接管完成：{len(adopt)} 个文件记为已应用")
                    return 0

                print(f"已应用 {len(done)} 个，待执行 {len(pending)} 个")
                for f in pending:
                    print(f"  待执行  {f.name}")
                if not pending:
                    return 0
                if args.dry_run:
                    print("\n--dry-run：没有改动任何东西")
                    return 0

                for f in pending:
                    statements = split_statements(f.read_text(encoding="utf-8"))
                    print(f"\n>> {f.name}（{len(statements)} 条）")
                    for stmt in statements:
                        head = " ".join(stmt.split())[:72]
                        try:
                            cur.execute(stmt)
                            print(f"   OK   {head}")
                        except Exception as exc:
                            # **不吞。** 半截迁移比不迁移更糟：库结构处在一个
                            # 谁都没设计过的中间态，而下一次运行会从这个文件的
                            # 第一条重来（它还没被记进跟踪表）。
                            sys.stderr.write(
                                f"\n   失败 {head}\n        {exc}\n\n"
                                f"迁移 {f.name} 中断。已执行的语句**不会回滚**"
                                f"（MySQL 的 DDL 是隐式提交的）。\n"
                                f"请手工确认这个文件已经执行到哪一步，修好之后重跑。\n"
                                f"⚠️ 不要把它记进 schema_migrations 来「跳过」——"
                                f"那会让库结构与代码的偏差永久留下。\n"
                            )
                            return 1

                    cur.execute(
                        "INSERT INTO schema_migrations (filename, applied_at, statements) "
                        "VALUES (%s, NOW(), %s)",
                        (f.name, len(statements)),
                    )
                    applied_count += 1
            finally:
                cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
    finally:
        conn.close()

    print(f"\n完成：本次执行 {applied_count} 个迁移文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
