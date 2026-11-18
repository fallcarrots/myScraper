"""包元数据测试：__version__ 来源与回退行为。"""

from __future__ import annotations

import importlib
import importlib.metadata

import pytest

import myscraper


def test_version_is_nonempty_str() -> None:
    """__version__ 是非空字符串。"""
    assert isinstance(myscraper.__version__, str)
    assert myscraper.__version__


def test_version_falls_back_when_distribution_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """包未安装（PackageNotFoundError）时回退到 0.0.0+unknown，随后恢复。"""

    def _raise_missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise_missing)
    try:
        importlib.reload(myscraper)
        assert myscraper.__version__ == "0.0.0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(myscraper)


def test_all_exports_version_only() -> None:
    """__all__ 仅导出 __version__。"""
    assert myscraper.__all__ == ["__version__"]
