"""clock 模块测试：SystemClock 的时区、单调性与非阻塞 sleep 契约。"""

from __future__ import annotations

import time
from datetime import timedelta

from fakes import FakeClock

from myscraper.clock import Clock, SystemClock


def test_now_is_tz_aware_utc() -> None:
    """SystemClock.now() 返回 tz-aware 的 UTC 时间。"""
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_monotonic_is_non_decreasing() -> None:
    """连续两次 monotonic 读数单调不减。"""
    clock = SystemClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second >= first


async def test_sleep_zero_returns_immediately() -> None:
    """sleep(0) 立即返回，不真实等待。"""
    clock = SystemClock()
    start = time.monotonic()
    await clock.sleep(0)
    assert time.monotonic() - start < 0.05


async def test_sleep_negative_returns_immediately() -> None:
    """sleep(-1) 立即返回，不真实等待。"""
    clock = SystemClock()
    start = time.monotonic()
    await clock.sleep(-1)
    assert time.monotonic() - start < 0.05


def test_system_clock_satisfies_protocol() -> None:
    """SystemClock 满足 Clock 协议。"""
    assert isinstance(SystemClock(), Clock)


def test_fake_clock_satisfies_protocol() -> None:
    """FakeClock 满足 Clock 协议。"""
    assert isinstance(FakeClock(), Clock)
