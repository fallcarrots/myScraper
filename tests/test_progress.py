"""progress 模块测试：全内存，零网络，不污染 stderr。"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import pytest

from myscraper.models import PageRecord
from myscraper.progress import NullProgress, RichProgress


def make_record(**overrides: Any) -> PageRecord:
    """构造一条最小合法 PageRecord，可用关键字参数覆盖任意字段。"""
    base: dict[str, Any] = {
        "url": "https://example.com/",
        "final_url": "https://example.com/final",
        "status_code": 200,
        "title": "Example Title",
        "text": "body text",
        "links": [],
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
        "status": "ok",
        "skip_reason": None,
        "error": None,
        "compliance": {},
    }
    base.update(overrides)
    return PageRecord(**base)


def test_null_progress_all_methods_noop() -> None:
    """NullProgress 的 start/advance/close 均可调用且无异常。"""
    p = NullProgress()
    p.start(10)
    p.advance(make_record())
    p.close()


def test_rich_progress_disabled_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """enabled=False 时全方法空操作，不向 stderr 写任何内容。"""
    p = RichProgress(enabled=False)
    p.start(3)
    p.advance(make_record())
    p.close()
    captured = capsys.readouterr()
    assert captured.err == ""


def test_rich_progress_full_lifecycle() -> None:
    """start(2) + 2 次 advance + close() 全程不抛异常。"""
    p = RichProgress(stream=io.StringIO(), enabled=True)
    p.start(2)
    p.advance(make_record())
    p.advance(make_record(status="skipped", skip_reason="robots"))
    p.close()


def test_close_is_idempotent() -> None:
    """close() 连续调用两次不抛异常。"""
    p = RichProgress(stream=io.StringIO(), enabled=True)
    p.start(1)
    p.advance(make_record())
    p.close()
    p.close()


def test_advance_counts_all_statuses() -> None:
    """advance 对 ok/failed/skipped 三种 status 都正常计数不抛异常。"""
    p = RichProgress(stream=io.StringIO(), enabled=True)
    p.start(3)
    p.advance(make_record(status="ok"))
    p.advance(make_record(status="failed", error="boom"))
    p.advance(make_record(status="skipped", skip_reason="robots"))
    p.close()
