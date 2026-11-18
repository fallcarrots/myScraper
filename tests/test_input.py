"""input 模块纯逻辑测试：normalize_input_url / dedupe / load_urls 与 InputError。"""

import io
from pathlib import Path

import pytest

from myscraper.errors import InputError
from myscraper.input import dedupe, load_urls, normalize_input_url


def test_normalize_adds_https_scheme() -> None:
    """无 scheme 的输入自动补 https://。"""
    assert normalize_input_url("example.com/a") == "https://example.com/a"


def test_normalize_keeps_http_scheme() -> None:
    """已带 http:// 的输入原样保留。"""
    assert normalize_input_url("http://a.example/x") == "http://a.example/x"


def test_normalize_strips_whitespace() -> None:
    """首尾空白被去除。"""
    assert normalize_input_url("  https://a.example/x  ") == "https://a.example/x"


@pytest.mark.parametrize("raw", ["", "#c"])
def test_normalize_empty_or_comment_is_none(raw: str) -> None:
    """空输入与 # 注释返回 None。"""
    assert normalize_input_url(raw) is None


def test_normalize_non_http_scheme_is_none() -> None:
    """非 http(s) scheme 返回 None。"""
    assert normalize_input_url("ftp://a.example/x") is None


def test_dedupe_preserves_order_and_first_occurrence() -> None:
    """按规范化键去重且保持首次出现顺序。"""
    urls = ["https://a.example/x", "https://a.example/x", "a.example/y"]
    result = dedupe(urls)
    assert len(result) == 2
    assert result[0] == "https://a.example/x"
    assert result[1] == "a.example/y"


def test_load_urls_from_file_skips_blank_and_comments(tmp_path: Path) -> None:
    """文件中的空行与 # 注释被跳过，返回有效 URL。"""
    path = tmp_path / "urls.txt"
    path.write_text(
        "https://a.example/1\n\n# 注释行\nhttps://a.example/2\n",
        encoding="utf-8",
    )
    assert load_urls(path) == ["https://a.example/1", "https://a.example/2"]


def test_load_urls_from_stdin_dash() -> None:
    """source='-' 时读取注入的 stdin 流。"""
    stdin = io.StringIO("a.example/1\n")
    assert load_urls("-", stdin=stdin) == ["https://a.example/1"]


def test_load_urls_missing_file_raises(tmp_path: Path) -> None:
    """不存在的文件抛 InputError。"""
    with pytest.raises(InputError):
        load_urls(tmp_path / "no-such-file.txt")


def test_load_urls_comment_only_file_raises(tmp_path: Path) -> None:
    """只有注释的文件未得到有效 URL，抛 InputError。"""
    path = tmp_path / "only-comments.txt"
    path.write_text("# 仅注释\n# 另一条注释\n", encoding="utf-8")
    with pytest.raises(InputError):
        load_urls(path)
