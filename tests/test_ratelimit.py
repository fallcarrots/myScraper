"""ratelimit 模块测试：compute_interval 限速公式 + HostRateLimiter 预约式等待。"""

import pytest
from fakes import FakeClock

from myscraper.ratelimit import HostRateLimiter, compute_interval


def test_compute_interval_rate_limit_dominates() -> None:
    """1/rate 低于最小间隔时，取 rate_limit_per_host 对应间隔。"""
    assert compute_interval(rate_limit_per_host=1.0, min_request_interval=0.5) == 1.0


def test_compute_interval_min_interval_dominates() -> None:
    """最小间隔高于 1/rate 时，取 min_request_interval。"""
    assert compute_interval(rate_limit_per_host=100.0, min_request_interval=0.5) == 0.5


def test_compute_interval_crawl_delay_dominates() -> None:
    """Crawl-delay 最大时，取 crawl_delay。"""
    assert (
        compute_interval(rate_limit_per_host=100.0, min_request_interval=0.5, crawl_delay=5.0)
        == 5.0
    )


def test_compute_interval_zero_rate_raises() -> None:
    """rate_limit_per_host <= 0 抛 ValueError。"""
    with pytest.raises(ValueError, match="rate_limit_per_host"):
        compute_interval(rate_limit_per_host=0.0, min_request_interval=0.5)


def make_limiter(clock: FakeClock) -> HostRateLimiter:
    """构造被测限速器：rate=1.0、min_interval=0.5，即每 host 间隔 1.0s。"""
    return HostRateLimiter(
        clock,
        rate_limit_per_host=1.0,
        min_request_interval=0.5,
    )


async def test_same_host_serial_acquires_sleep() -> None:
    """同 host 连续 3 次 acquire：首次不 sleep，后续各等待一个间隔。"""
    clock = FakeClock()
    limiter = make_limiter(clock)
    for _ in range(3):
        await limiter.acquire("example.com")
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


async def test_different_hosts_no_sleep() -> None:
    """不同 host 各 acquire 1 次：互不阻塞，不产生任何 sleep。"""
    clock = FakeClock()
    limiter = make_limiter(clock)
    await limiter.acquire("a.example.com")
    await limiter.acquire("b.example.com")
    assert clock.sleeps == []


async def test_single_acquire_advances_monotonic() -> None:
    """两次 acquire 后虚拟时间推进到 >= 1.0（一次完整间隔）。"""
    clock = FakeClock()
    limiter = make_limiter(clock)
    await limiter.acquire("example.com")
    await limiter.acquire("example.com")
    assert clock.monotonic() >= 1.0


# --- 全局 QPS 兜底 ---


async def test_default_no_global_slot_backward_compatible() -> None:
    """默认不启用全局槽：同 host 连发 3 次，间隔仍为 1.0s（向后兼容）。"""
    clock = FakeClock()
    limiter = HostRateLimiter(
        clock,
        rate_limit_per_host=1.0,
        min_request_interval=0.5,
    )
    for _ in range(3):
        await limiter.acquire("example.com")
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


async def test_global_slot_limits_across_hosts() -> None:
    """全局槽生效：两个不同 host 各 acquire 一次，第二次等待 1.0s。"""
    clock = FakeClock()
    limiter = HostRateLimiter(
        clock,
        rate_limit_per_host=1000.0,
        min_request_interval=0.0,
        global_rate_limit=1.0,
    )
    await limiter.acquire("a.example.com")
    await limiter.acquire("b.example.com")
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(1.0)


async def test_global_and_host_combined_by_max_not_sum() -> None:
    """两级限速以 max 复合：host 步长 1.0s 主导，全局 0.25s 不叠加。"""
    clock = FakeClock()
    limiter = HostRateLimiter(
        clock,
        rate_limit_per_host=1.0,
        min_request_interval=0.5,
        global_rate_limit=4.0,
    )
    await limiter.acquire("example.com")
    await limiter.acquire("example.com")
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_global_rate_limit_invalid_raises(bad: float) -> None:
    """global_rate_limit <= 0 抛 ValueError。"""
    clock = FakeClock()
    with pytest.raises(ValueError, match="global_rate_limit"):
        HostRateLimiter(
            clock,
            rate_limit_per_host=1.0,
            min_request_interval=0.5,
            global_rate_limit=bad,
        )


async def test_crawl_delay_participates_in_host_step() -> None:
    """crawl_delay 参与 host 步长：2.0s 的 crawl-delay 主导后续同 host 等待。"""
    clock = FakeClock()
    limiter = HostRateLimiter(
        clock,
        rate_limit_per_host=100.0,
        min_request_interval=0.0,
    )
    await limiter.acquire("a.example.com", crawl_delay=2.0)
    await limiter.acquire("a.example.com")
    assert clock.sleeps == [pytest.approx(2.0)]
