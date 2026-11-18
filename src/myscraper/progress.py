"""进度报告（rich 进度条，仅 stderr）。

设计要点：
- 数据走 stdout / 文件（output 层），进度可视化只写 **stderr**，两流彻底分离。
- :class:`NullProgress` 空实现：--json / 非 TTY / 测试场景下零输出。
- :class:`RichProgress` 基于 ``rich.progress.Progress``：描述列 + 进度条 +
  M/N 计数 + 耗时，右侧额外以 description 拼接 ok/failed/skipped 三项计数。
- ``enabled=False`` 时不创建任何 rich 对象，全部方法为空操作。
- ``close()`` 幂等，可安全重复调用。
"""

from __future__ import annotations

import sys
from typing import Protocol, TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from .models import PageRecord, RecordStatus

#: 进度条右侧状态计数的展示模板（ok/failed/skipped，取自 task.fields）。
_STATUS_FMT: str = (
    "ok={task.fields[ok]} failed={task.fields[failed]} skipped={task.fields[skipped]}"
)


class ProgressReporter(Protocol):
    """进度报告协议：start 声明总量，advance 逐条推进，close 收尾。"""

    def start(self, total: int) -> None:
        """开始一次进度会话，total 为待处理记录总数。"""
        ...

    def advance(self, record: PageRecord) -> None:
        """报告一条记录已处理完成（含 ok / failed / skipped）。"""
        ...

    def close(self) -> None:
        """结束进度会话并释放资源；幂等，可重复调用。"""
        ...


class NullProgress:
    """空实现：不输出任何内容（用于 --json / 非 TTY / 测试）。"""

    def start(self, total: int) -> None:
        """空操作。"""

    def advance(self, record: PageRecord) -> None:
        """空操作。"""

    def close(self) -> None:
        """空操作。"""


class RichProgress:
    """rich 进度条实现，只写 stderr（默认 ``sys.stderr``）。"""

    def __init__(self, *, stream: TextIO | None = None, enabled: bool = True) -> None:
        self._enabled = enabled
        self._ok = 0
        self._failed = 0
        self._skipped = 0
        self._closed = False
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        if not enabled:
            return
        console = Console(file=stream if stream is not None else sys.stderr, stderr=True)
        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn(_STATUS_FMT),
            TimeElapsedColumn(),
            console=console,
        )

    def start(self, total: int) -> None:
        """启动进度条并创建 total 大小的任务（初始化 ok/failed/skipped 字段）。"""
        if not self._enabled or self._progress is None or self._closed:
            return
        self._progress.start()
        self._task_id = self._progress.add_task("抓取中", total=total, ok=0, failed=0, skipped=0)

    def advance(self, record: PageRecord) -> None:
        """推进一格并按 record.status 累加 ok/failed/skipped 计数。"""
        if not self._enabled or self._progress is None or self._task_id is None:
            return
        status: RecordStatus = record.status
        if status == "ok":
            self._ok += 1
        elif status == "failed":
            self._failed += 1
        else:
            self._skipped += 1
        self._progress.update(
            self._task_id,
            advance=1,
            ok=self._ok,
            failed=self._failed,
            skipped=self._skipped,
        )

    def close(self) -> None:
        """停止进度条；幂等，重复调用不报错。"""
        if not self._enabled or self._closed:
            return
        self._closed = True
        if self._progress is not None:
            self._progress.stop()


__all__ = ["NullProgress", "ProgressReporter", "RichProgress"]
