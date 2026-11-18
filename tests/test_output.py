"""output 模块测试：全内存（io.StringIO / tmp_path），零网络。"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO

import pytest

from myscraper.errors import ConfigError
from myscraper.models import PageRecord
from myscraper.output import (
    CSV_COLUMNS,
    CsvWriter,
    JsonArrayWriter,
    JsonlWriter,
    open_writer,
    to_csv_row,
)

#: 与 models.PAGE_FIELDS 保持一致的声明顺序（测试 1 依赖）。
_EXPECTED_FIELDS: Final[tuple[str, ...]] = (
    "url",
    "final_url",
    "status_code",
    "title",
    "text",
    "links",
    "fetched_at",
    "status",
    "skip_reason",
    "error",
    "compliance",
)


def make_record(**overrides: object) -> PageRecord:
    """构造一条最小合法 PageRecord，可用关键字参数覆盖任意字段。"""
    base: dict[str, object] = {
        "url": "https://example.com/",
        "final_url": "https://example.com/final",
        "status_code": 200,
        "title": "Example Title",
        "text": "body text",
        "links": ["https://example.com/a", "https://example.com/b"],
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
        "status": "ok",
        "skip_reason": None,
        "error": None,
        "compliance": {"robots": "allowed", "license": "CC-BY-4.0"},
    }
    base.update(overrides)
    return PageRecord(**base)  # type: ignore[arg-type]


def _write_two_jsonl(stream: TextIO) -> None:
    """往给定流写 2 条 JSONL 记录并 close。"""
    writer = JsonlWriter(stream)
    writer.write(make_record())
    writer.write(make_record(url="https://example.com/two"))
    writer.close()


# ---------- A. to_csv_row 与列定义 ----------


def test_csv_columns_equals_page_fields_plus_links_count() -> None:
    """CSV_COLUMNS == (*PAGE_FIELDS, "links_count")，且顺序为模型声明序。"""
    from myscraper.models import PAGE_FIELDS

    assert (*PAGE_FIELDS, "links_count") == CSV_COLUMNS
    assert (*_EXPECTED_FIELDS, "links_count") == CSV_COLUMNS


def test_none_fields_become_empty_string() -> None:
    """None 字段 → ""；title=None 时 row["title"] == ""。"""
    record = make_record(title=None, skip_reason=None, error=None)
    row = to_csv_row(record)
    assert row["title"] == ""
    assert row["skip_reason"] == ""
    assert row["error"] == ""
    # 非 None 字段仍为 str
    assert row["url"] == "https://example.com/"
    assert all(isinstance(v, str) for v in row.values())


def test_links_serialized_as_json_and_count() -> None:
    """links 序列化为 JSON 字符串，links_count == str(len(links))。"""
    links: Final[list[str]] = ["https://a.example/", "https://b.example/", "https://c.example/"]
    row = to_csv_row(make_record(links=links))
    assert json.loads(row["links"]) == links
    assert isinstance(row["links"], str)
    assert row["links_count"] == "3"


def test_fetched_at_is_iso8601_parseable() -> None:
    """fetched_at 输出为 ISO-8601 字符串，可被 datetime.fromisoformat 解析。"""
    record = make_record(fetched_at=datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC))
    row = to_csv_row(record)
    parsed = datetime.fromisoformat(row["fetched_at"])
    assert parsed.year == 2026
    assert parsed.month == 1
    assert parsed.day == 1
    assert parsed.tzinfo is not None


def test_compliance_dict_sorted_and_correct_content() -> None:
    """compliance 用 sort_keys 序列化；json.loads 后内容正确。"""
    row = to_csv_row(make_record(compliance={"license": "MIT", "robots": "allowed"}))
    raw = row["compliance"]
    # sort_keys=True 保证键序确定（license < robots）
    assert raw.index('"license"') < raw.index('"robots"')
    assert json.loads(raw) == {"license": "MIT", "robots": "allowed"}


# ---------- B. 三个 writer ----------


def test_jsonl_writer_two_records() -> None:
    """JsonlWriter 写 2 条 → StringIO 2 行，每行 json.loads 成功且 url 正确。"""
    stream = io.StringIO()
    _write_two_jsonl(stream)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["url"] == "https://example.com/"
    assert second["url"] == "https://example.com/two"


def test_json_array_writer_two_records() -> None:
    """JsonArrayWriter 写 2 条后 close → json.loads 为长度 2 的 list。"""
    stream = io.StringIO()
    writer = JsonArrayWriter(stream)
    writer.write(make_record())
    writer.write(make_record(url="https://example.com/two"))
    writer.close()
    data = json.loads(stream.getvalue())
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["url"] == "https://example.com/"
    assert data[1]["url"] == "https://example.com/two"


def test_json_array_writer_empty_close() -> None:
    """不写任何记录直接 close → json.loads 为 []。"""
    stream = io.StringIO()
    writer = JsonArrayWriter(stream)
    writer.close()
    assert json.loads(stream.getvalue()) == []


def test_csv_writer_header_row_order() -> None:
    """CsvWriter 写 1 条后首行为表头，列顺序 == CSV_COLUMNS。"""
    stream = io.StringIO()
    writer = CsvWriter(stream)
    writer.write(make_record())
    writer.close()
    first_line = stream.getvalue().splitlines()[0]
    assert next(csv.reader(io.StringIO(first_line))) == list(CSV_COLUMNS)
    # 2 行：表头 + 1 数据
    assert len(stream.getvalue().splitlines()) == 2


def test_csv_writer_escapes_special_chars() -> None:
    """含 , " \\n 的 text 字段写入后用 DictReader 读回，值完全一致（含换行）。"""
    tricky: Final[str] = 'has,comma "and quote"\nand newline'
    stream = io.StringIO()
    writer = CsvWriter(stream)
    writer.write(make_record(text=tricky))
    writer.close()
    stream.seek(0)
    rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["text"] == tricky


# ---------- C. open_writer 工厂 ----------


def test_open_writer_jsonl_to_file(tmp_path: Path) -> None:
    """open_writer("jsonl", tmp_path/"a.jsonl") 写 2 条 → 文件 2 行。"""
    target = tmp_path / "a.jsonl"
    with open_writer("jsonl", target) as writer:
        writer.write(make_record())
        writer.write(make_record(url="https://example.com/two"))
    content = target.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["url"] == "https://example.com/"
    assert json.loads(lines[1])["url"] == "https://example.com/two"


def test_open_writer_csv_utf8_bom(tmp_path: Path) -> None:
    """open_writer("csv", ..., csv_utf8_bom=True) → 文件以 \\ufeff 开头。"""
    target = tmp_path / "a.csv"
    with open_writer("csv", target, csv_utf8_bom=True) as writer:
        writer.write(make_record())
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    decoded = target.read_text(encoding="utf-8-sig")
    assert not decoded.startswith("\ufeff")
    assert decoded.startswith("url,final_url,status_code")


def test_open_writer_xml_raises_config_error() -> None:
    """open_writer("xml", None) → pytest.raises(ConfigError)。"""
    with pytest.raises(ConfigError), open_writer("xml", None):
        pass


def test_open_writer_jsonl_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """open_writer("jsonl", None) → 写 stdout（数据与进度分离前提）。"""
    with open_writer("jsonl", None) as writer:
        writer.write(make_record())
        writer.write(make_record(url="https://example.com/two"))
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["url"] == "https://example.com/"
    assert json.loads(lines[1])["url"] == "https://example.com/two"
