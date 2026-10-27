"""放量用量报告（scripts/usage_report.py）的纯逻辑测试

只测两处真有逻辑的地方，别的都是打印：

- `peak_concurrency`：把每次请求还原成区间后求瞬时峰值。这是整份报告里唯一
  一个「答案对不对影响决策」的数 —— 它决定「2C 8G 扛不扛得住」，算错一倍就
  是放量放错。
- `parse_dsn`：密码里带 `@` 的连接串。migrate.py 在 108 上踩过一次，同一个
  函数抄过来就得带着同一条用例，否则下次是在生产上重新发现。

不连库：`fetch` 只是一条 SELECT，测它等于测 pymysql。
"""

import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import usage_report as ur  # noqa: E402


def _row(end: str, latency_ms: int, **over):
    base = {
        "created_at": datetime.strptime(end, "%Y-%m-%d %H:%M:%S"),
        "latency_ms": latency_ms,
        "user_uid": "u1",
        "dept_id": 1,
        "model_name": "m",
        "status": "success",
        "error_code": None,
        "stream": 1,
        "aborted": 0,
        "call_kind": "chat",
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cached_prompt_tokens": 900,
        "cost": "0.01",
    }
    base.update(over)
    return base


class TestPeakConcurrency:
    def test_non_overlapping_requests_never_exceed_one(self):
        rows = [
            _row("2026-09-16 10:00:05", 5000),   # 10:00:00 → 10:00:05
            _row("2026-09-16 10:00:15", 5000),   # 10:00:10 → 10:00:15
        ]
        peak, _, _ = ur.peak_concurrency(rows)
        assert peak == 1

    def test_overlapping_requests_stack(self):
        # 三条都覆盖 10:00:08 这一瞬间
        rows = [
            _row("2026-09-16 10:00:10", 10_000),  # 10:00:00 →
            _row("2026-09-16 10:00:12", 10_000),  # 10:00:02 →
            _row("2026-09-16 10:00:14", 10_000),  # 10:00:04 →
        ]
        peak, at, _ = ur.peak_concurrency(rows)
        assert peak == 3
        assert at == datetime(2026, 9, 16, 10, 0, 4)

    def test_long_request_counts_for_its_whole_duration(self):
        """一条 120 秒的慢请求要和它跨越的每一条短请求都算重叠。

        按 created_at 分桶的算法会把它只记在结束的那一秒上 —— 那正是这个函数
        存在的理由。
        """
        rows = [_row("2026-09-16 10:02:00", 120_000)]  # 10:00:00 → 10:02:00
        rows += [_row("2026-09-16 10:%02d:30" % m, 1000) for m in range(0, 2)]
        peak, _, _ = ur.peak_concurrency(rows)
        assert peak == 2

    def test_zero_and_null_latency_do_not_crash(self):
        rows = [_row("2026-09-16 10:00:00", 0), _row("2026-09-16 10:00:00", None)]
        peak, _, _ = ur.peak_concurrency(rows)
        assert peak >= 0

    def test_hourly_peaks_are_reported_per_hour(self):
        rows = [
            _row("2026-09-16 10:00:10", 10_000),
            _row("2026-09-16 10:00:12", 10_000),
            _row("2026-09-16 11:00:10", 1000),
        ]
        _, _, by_hour = ur.peak_concurrency(rows)
        assert by_hour["2026-09-16 10"] == 2
        assert by_hour["2026-09-16 11"] == 1


class TestParseDsn:
    def test_password_containing_at_sign(self):
        # 手搓 ([^@]+) 的正则会在这里切错，而报出来的是 Access denied
        got = ur.parse_dsn("mysql://root:p%40ss%3Aword@10.0.0.1:32334/audit")
        assert got["user"] == "root"
        assert got["password"] == "p@ss:word"
        assert got["host"] == "10.0.0.1"
        assert got["port"] == 32334
        assert got["database"] == "audit"

    def test_default_port(self):
        assert ur.parse_dsn("mysql://u:p@h/db")["port"] == 3306

    def test_missing_host_is_fatal(self):
        with pytest.raises(SystemExit):
            ur.parse_dsn("mysql:///db")


class TestReport:
    def test_empty_window_says_so_instead_of_dividing_by_zero(self, capsys):
        ur.report([], date(2026, 9, 16), date(2026, 9, 18), 300, 0.4, 0.25)
        assert "没有任何调用记录" in capsys.readouterr().out

    def test_peak_hour_share_is_against_that_days_own_total(self, capsys):
        """份额的分母是**那一天自己的**总量。

        用全窗口日均当分母时，一天忙、其余天闲必然算出 >100%，而那个数会被
        直接读成「峰值小时占全天 148%」。
        """
        rows = [_row("2026-09-16 17:%02d:00" % m, 1000) for m in range(0, 30)]
        rows += [_row("2026-09-16 09:%02d:00" % m, 1000) for m in range(0, 10)]
        ur.report(rows, date(2026, 9, 16), date(2026, 9, 18), 300, 0.4, 0.25)
        out = capsys.readouterr().out
        assert "占当天 40 次的 75%" in out

    def test_failing_model_is_flagged(self, capsys):
        rows = [_row("2026-09-16 10:%02d:00" % m, 1000, model_name="good") for m in range(30)]
        rows += [
            _row("2026-09-16 11:%02d:00" % m, 1000, model_name="bad",
                 status="failed", error_code="upstream_404")
            for m in range(25)
        ]
        ur.report(rows, date(2026, 9, 16), date(2026, 9, 18), 300, 0.4, 0.25)
        out = capsys.readouterr().out
        assert "upstream_404×25" in out
        # 样本够大且失败率超 5% 的那一行要带标记，好模型那行不能带
        bad_line = next(l for l in out.splitlines() if l.strip().startswith("bad"))
        good_line = next(l for l in out.splitlines() if l.strip().startswith("good"))
        assert "⚠️" in bad_line
        assert "⚠️" not in good_line

    def test_small_sample_is_not_flagged(self, capsys):
        """4 次调用全失败也不打标记 —— 样本太小，打了就是天天在喊狼来了。

        2026-09-08 的备份里豆包正好是 4 次全 404（那是已修的地址拼接 bug）。
        """
        rows = [
            _row("2026-09-16 10:%02d:00" % m, 1000, model_name="rare",
                 status="failed", error_code="upstream_404")
            for m in range(4)
        ]
        ur.report(rows, date(2026, 9, 16), date(2026, 9, 18), 300, 0.4, 0.25)
        rare_line = next(
            l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("rare")
        )
        assert "⚠️" not in rare_line
