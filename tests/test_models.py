"""PageRecord 模型与 PAGE_FIELDS、MissingRequired 的行为测试。

覆盖：默认值、tz-aware 校验、extra="forbid"、frozen=True、字段集合合规快照、
PAGE_FIELDS 顺序契约，以及 MissingRequired 消息不泄漏尖括号（防注入标记）。
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from myscraper.errors import MissingRequired
from myscraper.models import PAGE_FIELDS, PageRecord

#: 约定的 11 个合法字段名（与 models.py 声明顺序一致）。
_EXPECTED_FIELDS: frozenset[str] = frozenset(
    {
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
    }
)

#: 合规红线：默认字段集禁止出现的 PII 字段名。
_FORBIDDEN_PII_FIELDS: tuple[str, ...] = ("author", "email", "phone", "ip", "user_id")


def make_record(**overrides: Any) -> PageRecord:
    """构造一条合法的 PageRecord，供各测试按需覆盖字段。

    Args:
        overrides: 覆盖默认 kwargs 的字段映射。

    Returns:
        通过全部校验的 PageRecord 实例。
    """
    defaults: dict[str, Any] = {
        "url": "https://example.com/a",
        "final_url": "https://example.com/a",
        "status_code": 200,
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    merged: dict[str, Any] = {**defaults, **overrides}
    return PageRecord(**merged)


def test_valid_record_defaults() -> None:
    """合法构造成功：可选字段取默认值。"""
    rec = make_record()
    assert rec.status == "ok"
    assert rec.text == ""
    assert rec.links == []
    assert rec.title is None


def test_naive_datetime_rejected() -> None:
    """naive datetime 缺时区信息，构造时必须抛 ValidationError。"""
    with pytest.raises(ValidationError):
        make_record(fetched_at=datetime(2026, 1, 1))


def test_extra_field_forbidden() -> None:
    """extra="forbid"：注入未声明字段 author 必须抛 ValidationError。"""
    with pytest.raises(ValidationError):
        make_record(author="x")


def test_frozen_immutable() -> None:
    """frozen=True：对实例字段赋值必须抛 ValidationError。"""
    rec = make_record()
    with pytest.raises(ValidationError):
        rec.title = "x"  # type: ignore[misc]


def test_field_set_snapshot() -> None:
    """字段集合恰为 11 个约定字段，且不含任何 PII 字段。"""
    assert set(PageRecord.model_fields) == _EXPECTED_FIELDS
    for pii in _FORBIDDEN_PII_FIELDS:
        assert pii not in PageRecord.model_fields


def test_page_fields_contract() -> None:
    """PAGE_FIELDS 为 tuple、长度 11、顺序与 model_fields 一致。"""
    fields: object = PAGE_FIELDS
    assert isinstance(fields, tuple)
    assert len(PAGE_FIELDS) == 11
    assert tuple(PageRecord.model_fields) == PAGE_FIELDS


def test_missing_required_message() -> None:
    """MissingRequired 消息不含尖括号，且包含 slot 与 selector。"""
    msg = str(MissingRequired(slot="title", selector="h1.t"))
    assert "<" not in msg
    assert ">" not in msg
    assert "title" in msg
    assert "h1.t" in msg
