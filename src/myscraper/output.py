"""结果流式输出（内存 O(1)）。

设计要点：
- 逐条写出，不在内存中累积记录列表，写入 N 条记录仅占用常数内存。
- JSONL 每行一个完整 JSON 对象；JSON 以流式方式拼装数组（首条前写 ``[\\n``，
  记录之间写 ``,\\n``，close 时补 ``\\n]\\n``），从不整体 ``json.dump``。
- CSV 表头固定来自 :data:`~.models.PAGE_FIELDS`（即 ``PageRecord.model_fields``
  的声明顺序）追加 ``links_count`` 列，不依赖首条数据——即使结果集为空也
  保证有且仅有一行表头。
- 三个 writer 均接收已打开的可写文本流，只负责写与 flush，不拥有流；
  由 :func:`open_writer` 工厂负责打开 / 关闭文件并统一 close。
"""

from __future__ import annotations

import contextlib
import csv
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol, TextIO, runtime_checkable

from .errors import ConfigError
from .models import PAGE_FIELDS, PageRecord

#: JSONL 每写多少条记录执行一次 flush。
_FLUSH_EVERY: Final[int] = 100

#: CSV 列名：PageRecord 全部字段（固定声明顺序）+ 派生列 links_count。
CSV_COLUMNS: Final[tuple[str, ...]] = (*PAGE_FIELDS, "links_count")


@runtime_checkable
class RecordWriter(Protocol):
    """输出记录写入器协议：逐条写 + 结束时收尾。"""

    def write(self, record: PageRecord) -> None:
        """写出一条抓取记录。"""
        ...

    def close(self) -> None:
        """收尾（flush / 补数组闭合 / flush 表头缓冲），不关闭底层文件。"""
        ...


def to_csv_row(record: PageRecord) -> dict[str, str]:
    """把一条记录转为 CSV 行（全部值为字符串，固定列序）。

    - ``None`` → ``""``；``datetime`` → UTC ISO-8601；``bool``/``int``/``str`` 原样。
    - ``links``：单列，``json.dumps(..., ensure_ascii=False)``，不换行拼接。
    - ``compliance``：``json.dumps(..., ensure_ascii=False, sort_keys=True)``。
    - 追加派生列 ``links_count``。
    """
    row: dict[str, str] = {}
    for name in PAGE_FIELDS:
        value = getattr(record, name)
        if value is None:
            row[name] = ""
        elif name == "links":
            row[name] = json.dumps(value, ensure_ascii=False)
        elif name == "compliance":
            row[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        elif isinstance(value, datetime):
            row[name] = value.isoformat()
        else:
            row[name] = str(value)
    row["links_count"] = str(len(record.links))
    return row


class JsonlWriter:
    """JSONL 写入器：每条记录一行 JSON，每 100 条 flush 一次。"""

    __slots__ = ("_count", "_stream")

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._count = 0

    def write(self, record: PageRecord) -> None:
        """写出一条 JSONL 行。"""
        self._stream.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False))
        self._stream.write("\n")
        self._count += 1
        if self._count % _FLUSH_EVERY == 0:
            self._stream.flush()

    def close(self) -> None:
        """flush 缓冲。"""
        self._stream.flush()


class JsonArrayWriter:
    """JSON 数组流式写入器：``[`` + 逗号分隔记录 + ``]``。

    空结果集时 ``close()`` 输出 ``[]\\n``（合法空数组）。
    """

    __slots__ = ("_count", "_stream")

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._count = 0

    def write(self, record: PageRecord) -> None:
        """写出一条记录；首条前写 ``[\\n``，其后用 ``,\\n`` 分隔。"""
        if self._count == 0:
            self._stream.write("[\n")
        else:
            self._stream.write(",\n")
        self._stream.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False))
        self._count += 1

    def close(self) -> None:
        """补数组闭合并 flush；从未写过记录时输出 ``[]\\n``。"""
        if self._count == 0:
            self._stream.write("[]\n")
        else:
            self._stream.write("\n]\n")
        self._stream.flush()


class CsvWriter:
    """CSV 写入器：构造时立即写表头（不依赖首条数据）。"""

    __slots__ = ("_stream", "_writer")

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._writer = csv.DictWriter(
            stream,
            fieldnames=CSV_COLUMNS,
            extrasaction="ignore",
            lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        self._writer.writeheader()

    def write(self, record: PageRecord) -> None:
        """写出一行 CSV 记录。"""
        self._writer.writerow(to_csv_row(record))

    def close(self) -> None:
        """flush 缓冲。"""
        self._stream.flush()


def _make_writer(fmt: str, stream: TextIO) -> RecordWriter:
    """根据 ``fmt`` 构造对应的 writer（``fmt`` 已由调用方校验）。"""
    if fmt == "jsonl":
        return JsonlWriter(stream)
    if fmt == "json":
        return JsonArrayWriter(stream)
    if fmt == "csv":
        return CsvWriter(stream)
    raise ConfigError(f"不支持的输出格式: {fmt}")


@contextmanager
def open_writer(
    fmt: str,
    path: Path | None,
    *,
    csv_utf8_bom: bool = False,
) -> Iterator[RecordWriter]:
    """打开目标并返回对应 writer 的上下文工厂。

    - ``path is None`` 或 ``str(path) == "-"`` → 写 ``sys.stdout``（不负责关闭）。
    - 否则 ``open(path, "w", encoding="utf-8", newline="")`` 打开；
      ``csv_utf8_bom=True`` 时用 ``encoding="utf-8-sig"``。
    - ``fmt`` 只接受 ``jsonl`` / ``json`` / ``csv``，否则抛 :class:`ConfigError`。
    - ``finally`` 中 close writer，并仅关闭自己打开的文件句柄。
    """
    encoding = "utf-8-sig" if (fmt == "csv" and csv_utf8_bom) else "utf-8"
    if fmt not in ("jsonl", "json", "csv"):
        raise ConfigError(f"不支持的输出格式: {fmt}")
    with contextlib.ExitStack() as stack:
        stream: TextIO
        if path is None or str(path) == "-":
            stream = sys.stdout
        else:
            stream = stack.enter_context(open(path, "w", encoding=encoding, newline=""))
        writer: RecordWriter = _make_writer(fmt, stream)
        try:
            yield writer
        finally:
            writer.close()


__all__ = [
    "CSV_COLUMNS",
    "CsvWriter",
    "JsonArrayWriter",
    "JsonlWriter",
    "RecordWriter",
    "open_writer",
    "to_csv_row",
]
