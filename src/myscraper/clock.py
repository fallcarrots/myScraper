"""时间源抽象：统一 now / monotonic / sleep 的可注入实现。

**本文件是全仓库唯一允许出现 `asyncio.sleep` 的地方**（CI 有扫描断言）：
其余代码一律通过注入的 `Clock` 协议等待，以保证限速/退避可测试、可替换。
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """可注入的时间源协议：测试中可用假时钟替换真实实现。"""

    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """基于系统时间的真实 Clock 实现。"""

    def now(self) -> datetime:
        """返回当前 tz-aware UTC 时间。"""
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        """返回单调时钟读数（秒），用于测量 elapsed/超时。"""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """异步等待 seconds 秒；seconds <= 0 时直接返回，不调用 asyncio.sleep。"""
        if seconds <= 0:
            return
        await asyncio.sleep(seconds)
