"""测试用假时钟：虚拟时间推进，`sleep` 仅记录、绝不真实等待。"""

from datetime import UTC, datetime, timedelta

from myscraper.clock import Clock

__all__ = ["FakeClock"]


class FakeClock:
    """可编程时钟：sleep 只记录并推进虚拟时间，绝不真睡。"""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self._sleeps: list[float] = []

    @property
    def sleeps(self) -> list[float]:
        """返回已记录 sleep 时长的副本，避免外部修改内部状态。"""
        return list(self._sleeps)

    def now(self) -> datetime:
        """返回虚拟时间对应的 tz-aware UTC 时间。"""
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.monotonic())

    def monotonic(self) -> float:
        """返回当前虚拟时间（秒）。"""
        return self._t

    async def sleep(self, seconds: float) -> None:
        """记录 seconds 并推进虚拟时间；seconds <= 0 也会被记录。"""
        self._sleeps.append(seconds)
        self._t += seconds

    def advance(self, seconds: float) -> None:
        """仅推进虚拟时间，不记录为 sleep。"""
        self._t += seconds


# 结构子类型断言：确保 FakeClock 满足 Clock 协议（mypy strict）。
_clock_check: Clock = FakeClock()
