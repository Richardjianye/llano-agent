"""容器时区自检 —— 2026-09-03

`now()` 存的是本地时间，而在这次之前没有任何一处保证容器时区是东八区：
镜像没设 TZ、部署平台也没配，库里于是全是 UTC，后台每处时间早 8 小时。
这类偏差不会报错，所以自检得自己喊出来。
"""

from __future__ import annotations

import time

import main
import pytest


@pytest.fixture(autouse=True)
def _restore_process_timezone():
    """time.tzset() 改的是**进程级**状态，monkeypatch 只还得了环境变量。

    不还回来的话，这个文件跑完之后同一进程里所有用到本地时间的用例都在
    纽约时区下跑 —— 而它们不会说自己为什么挂，只会挂在别处。
    """
    yield
    time.tzset()


def test_a_shanghai_container_stays_quiet(monkeypatch):
    """+8 时不该有任何告警 —— 每次启动都刷一条会让人学会忽略它。"""
    said = []
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    monkeypatch.setattr(main.logger, "warning", lambda msg: said.append(msg))

    main.assert_timezone()

    assert said == []


def test_a_utc_container_says_so_loudly(monkeypatch):
    """⚠️ 这条是本次事故的回归：容器跑在 UTC 时必须留下一行日志。

    断言里带上「东八区」这几个字，是因为这行日志的唯一读者是运维，
    而他要的是「该设什么」，不是「有点不对」。
    """
    said = []
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr(main.logger, "warning", lambda msg: said.append(msg))

    main.assert_timezone()

    assert len(said) == 1
    assert "东八区" in said[0]
    assert "TZ=Asia/Shanghai" in said[0]


def test_the_check_never_blocks_startup(monkeypatch):
    """时区不对是数据口径问题，不是安全问题：不能把服务拦在门外。"""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    monkeypatch.setattr(main.logger, "warning", lambda msg: None)

    main.assert_timezone()  # 不抛就是通过
