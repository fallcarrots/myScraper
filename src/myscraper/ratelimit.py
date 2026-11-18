"""每主机限速：全项目唯一的限速公式与预约式等待实现。

等待一律通过注入的 `Clock`（本文件不直接使用 `asyncio.sleep`，见 clock.py 的约定）。
锁内只计算时间戳、锁外睡眠，从而同 host 串行、跨 host 并行。
"""

import asyncio

from .clock import Clock

__all__ = ["HostRateLimiter", "compute_interval"]


def compute_interval(
    *,
    rate_limit_per_host: float,
    min_request_interval: float,
    crawl_delay: float | None = None,
) -> float:
    """计算单主机两次请求之间的最小间隔（秒）。

    这是**全项目唯一的限速公式**：返回
    ``max(1 / rate_limit_per_host, min_request_interval, crawl_delay or 0.0)``，
    其余模块不得另行实现等价逻辑。

    Args:
        rate_limit_per_host: 每主机每秒最大请求数，必须为正数。
        min_request_interval: 两次请求之间的硬性最小间隔（秒）。
        crawl_delay: robots.txt 的 Crawl-delay（秒），可为 None 表示不生效。

    Returns:
        取三者（crawl_delay 缺省时取前两者）最大值后的最小间隔秒数。

    Raises:
        ValueError: 当 ``rate_limit_per_host <= 0`` 时。
    """
    if rate_limit_per_host <= 0:
        raise ValueError("rate_limit_per_host 必须为正数")
    return max(1.0 / rate_limit_per_host, min_request_interval, crawl_delay or 0.0)


class HostRateLimiter:
    """按主机串行、跨主机并行的异步限速器。

    预约式实现：每次 acquire 在该 host 的锁内读取单调时钟并预约下一个允许时刻，
    然后释放锁、在锁外通过注入的 `Clock` 等待到预约点。因此同一 host 的请求
    串行排队，不同 host 的等待互不阻塞。
    """

    def __init__(
        self,
        clock: Clock,
        *,
        rate_limit_per_host: float,
        min_request_interval: float,
    ) -> None:
        """初始化限速器。

        Args:
            clock: 注入的时间源，提供 monotonic 与 sleep。
            rate_limit_per_host: 每主机每秒最大请求数（正数）。
            min_request_interval: 两次请求之间的硬性最小间隔（秒）。
        """
        if rate_limit_per_host <= 0:
            raise ValueError("rate_limit_per_host 必须为正数")
        self._clock = clock
        self._rate_limit_per_host = rate_limit_per_host
        self._min_request_interval = min_request_interval
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, host: str, crawl_delay: float | None = None) -> None:
        """预约并等待到该 host 下一次允许请求的时刻。

        步骤：
        1. 由 `compute_interval` 计算本次间隔 step（全项目唯一限速公式）。
        2. 取该 host 的 `asyncio.Lock`（``dict`` + ``setdefault``，无锁创建）。
        3. **锁内**只做预约：读取单调时钟 now，计算需等待的 delay，
           并把该 host 的下一允许时刻推进为 ``now + delay + step``。
        4. **锁外**等待：delay > 0 时才 ``await clock.sleep(delay)``，
           delay 为 0 时不调用 sleep。

        锁内只计算时间戳、锁外睡眠，从而同 host 串行、跨 host 并行。

        Args:
            host: 目标主机名。
            crawl_delay: 该 host 的 robots.txt Crawl-delay（秒），可为 None。
        """
        step = compute_interval(
            rate_limit_per_host=self._rate_limit_per_host,
            min_request_interval=self._min_request_interval,
            crawl_delay=crawl_delay,
        )
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock.monotonic()
            delay = max(0.0, self._next.get(host, now) - now)
            self._next[host] = now + delay + step
        if delay > 0:
            await self._clock.sleep(delay)
