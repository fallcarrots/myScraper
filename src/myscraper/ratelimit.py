"""每主机限速 + 全局速率兜底：全项目唯一的限速公式与预约式等待实现。

等待一律通过注入的 `Clock`（本文件不直接使用 `asyncio.sleep`，见 clock.py 的约定）。
单把预约锁内只计算时间戳（零 await）、锁外睡眠一次，从而同 host 串行、
跨 host 并行，且可选的全局槽位约束多 host 聚合压力。
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
    """按主机串行、跨主机并行的异步限速器，可选全局速率兜底槽位。

    预约式实现：每次 acquire 在单把预约锁内读取单调时钟并预约下一个允许时刻，
    然后释放锁、在锁外通过注入的 `Clock` 等待到预约点。因此同一 host 的请求
    串行排队，不同 host 的等待互不阻塞；启用全局槽位时，所有 host 的聚合
    请求速率额外受 ``global_rate_limit`` 约束。

    三条不变量：
    1. `compute_interval` 是全项目唯一的 rate→秒 换算点：全局步长在
       ``__init__`` 预计算，host 步长在 acquire 的临界区内调用它计算
       （纯计算、零 await），任何地方不得手写等价算式。
    2. 两级限速（host 与全局）的复合方式**仅为 ``max()``**：不得串联两次
       acquire、不得把两级 delay 相加。
    3. 单锁临界区零 await → 无锁嵌套、无死锁面。

    全局游标取舍（Y-8）：全局槽按「占用」记账
    （``max(_next_g, now) + _step_g``），不随某个 host 的等待时长后移，
    否则慢 host 会把全局游标推后、让其它 host 长期无槽而饿死。代价是
    慢 host 等待期间全局可能少计约 1 个在途请求，其偏差上界由
    ``max_concurrency`` 决定。

    crawl_delay 为 per-host 粘性：某 host 一旦传入过正的 Crawl-delay，
    即记录为该 host 的历史最大值；后续 acquire 即使漏传，也不会退回
    普通步长（合规要求）。
    """

    def __init__(
        self,
        clock: Clock,
        *,
        rate_limit_per_host: float,
        min_request_interval: float,
        global_rate_limit: float | None = None,
    ) -> None:
        """初始化限速器。

        Args:
            clock: 注入的时间源，提供 monotonic 与 sleep。
            rate_limit_per_host: 每主机每秒最大请求数（正数）。
            min_request_interval: 两次请求之间的硬性最小间隔（秒）。
            global_rate_limit: 全局每秒最大请求数（正数），None 表示不启用
                全局槽位（完全向后兼容）。

        Raises:
            ValueError: 当 ``rate_limit_per_host <= 0``，或
                ``global_rate_limit`` 非 None 且 ``<= 0`` 时。
        """
        if rate_limit_per_host <= 0:
            raise ValueError("rate_limit_per_host 必须为正数")
        if global_rate_limit is not None and global_rate_limit <= 0:
            raise ValueError("global_rate_limit 必须为正数")
        self._clock = clock
        self._rate_h = rate_limit_per_host
        self._min_interval = min_request_interval
        self._step_g = (
            compute_interval(rate_limit_per_host=global_rate_limit, min_request_interval=0.0)
            if global_rate_limit is not None
            else 0.0
        )
        self._lock = asyncio.Lock()
        self._next_host: dict[str, float] = {}
        self._crawl_delay: dict[str, float] = {}
        self._next_g: float = 0.0

    async def acquire(self, host: str, crawl_delay: float | None = None) -> None:
        """预约并等待到该 host 下一次允许请求的时刻（含全局兜底）。

        步骤：
        1. host 归一（casefold），避免同一站点被拆成两个桶导致速率翻倍。
        2. crawl_delay 按 host 粘性记录：取历史最大值，后续漏传也不退回
           普通步长。
        3. **锁内**只做预约（零 await）：调用唯一换算函数 `compute_interval`
           得到 host 步长（含粘性 crawl_delay），读取单调时钟 now，分别
           计算 host 与全局需等待的 d_h、d_g，取 ``delay = max(d_h, d_g)``；
           host 下一允许时刻推进为 ``now + delay + step_h``，全局游标按
           「占用」记账推进为 ``max(_next_g, now) + _step_g``（Y-8：不随
           慢 host 的等待时长后移，代价是等待期间全局可能少计约 1 个在途
           请求，上界由 ``max_concurrency`` 决定）。
        4. **锁外**等待：delay > 0 时才 ``await clock.sleep(delay)``，
           且只 sleep 一次；delay 为 0 时不调用 sleep。

        不变量：
        1. `compute_interval` 是全项目唯一 rate→秒 换算点（临界区内调用，
           纯计算、零 await）。
        2. 两级限速的复合方式**仅为 ``max()``**，不得串联两次 acquire、
           不得相加。
        3. 单锁临界区零 await → 无锁嵌套、无死锁面。

        Args:
            host: 目标主机名（内部按 casefold 归一）。
            crawl_delay: 该 host 的 robots.txt Crawl-delay（秒），可为 None；
                正值将按 host 粘性保留（历史最大值）。
        """
        host = host.casefold()
        delay = 0.0
        async with self._lock:
            if crawl_delay is not None and crawl_delay > 0.0:
                prev = self._crawl_delay.get(host, 0.0)
                self._crawl_delay[host] = max(prev, crawl_delay)
            step_h = compute_interval(
                rate_limit_per_host=self._rate_h,
                min_request_interval=self._min_interval,
                crawl_delay=self._crawl_delay.get(host),
            )
            now = self._clock.monotonic()
            if len(self._next_host) > 1024:
                horizon = now - 60.0
                self._next_host = {h: t for h, t in self._next_host.items() if t >= horizon}
            d_h = max(0.0, self._next_host.get(host, now) - now)
            d_g = max(0.0, self._next_g - now) if self._step_g > 0.0 else 0.0
            delay = max(d_h, d_g)  # 两级复合只允许 max()
            self._next_host[host] = now + delay + step_h
            if self._step_g > 0.0:
                # 全局槽按"占用"记账，不随 host 的等待时长后移，否则慢 host 会让其它 host 长期无槽。
                self._next_g = max(self._next_g, now) + self._step_g
        if delay > 0:
            await self._clock.sleep(delay)  # 锁外只 sleep 一次
