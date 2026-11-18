"""URL 输入的规范化、去重与加载。

只依赖标准库与 ``.errors.InputError``，负责把用户提供的原始输入
（单条 URL 字符串、URL 列表、文件或 stdin）清洗为干净的绝对 URL 列表。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from .errors import InputError

__all__ = ["dedupe", "load_urls", "normalize_input_url"]

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def normalize_input_url(raw: str) -> str | None:
    """把单条原始输入规范化为绝对 http(s) URL。

    规则：
        - strip 后为空或以 ``#`` 开头（注释）→ 返回 None；
        - 已带 scheme 时（匹配 ``_SCHEME_RE``），scheme 必须是
          ``http``/``https``（大小写不敏感），否则返回 None；
        - 不带 scheme 时自动补 ``https://`` 前缀；
        - 拆解后 scheme 必须是 http/https 且 hostname 非空，否则返回 None。

    Args:
        raw: 用户输入的单条 URL 原始字符串。

    Returns:
        规范化后的绝对 URL；无法解析为合法 http(s) URL 时返回 None。
        本函数保证不抛出任何异常。
    """
    try:
        text = raw.strip()
        if not text or text.startswith("#"):
            return None
        if _SCHEME_RE.match(text):
            scheme = urlsplit(text).scheme.lower()
            if scheme not in ("http", "https"):
                return None
        else:
            text = f"https://{text}"
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return text
    except Exception:  # pragma: no cover - 防御性兜底，保证永不抛异常
        return None


def _normalized_key(url: str) -> str | None:
    """返回用于去重比较的规范化键，无效输入返回 None。

    Args:
        url: 待规范化的单条 URL。

    Returns:
        规范化后的 URL 字符串，无效时为 None。
    """
    return normalize_input_url(url)


def dedupe(urls: Iterable[str]) -> list[str]:
    """按规范化结果保序去重。

    以 :func:`normalize_input_url` 的返回值作为去重键，无法规范化的条目
    （返回 None）会被跳过；相同键只保留首次出现的原始顺序。

    Args:
        urls: 任意可迭代的 URL 字符串集合。

    Returns:
        去重后的 URL 列表，保持首次出现顺序。
    """
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        key = _normalized_key(url)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def _iter_source_lines(source: str | Path | None, stdin: TextIO | None) -> Iterator[str]:
    """逐行产出 source 中的原始文本行。

    source 为 None 或 ``"-"`` 时读 stdin（默认 ``sys.stdin``），否则按
    UTF-8（带 BOM 容忍）读取 ``Path(source)``。读取失败抛 InputError。

    Args:
        source: 文件路径、``"-"`` 或 None。
        stdin: 可注入的标准输入流，用于测试。

    Yields:
        文件/标准输入中的每一行（含换行符）。

    Raises:
        InputError: 无法读取指定的 URL 文件时抛出。
    """
    stream: TextIO
    if source is None or (isinstance(source, str) and source == "-"):
        stream = stdin if stdin is not None else sys.stdin
        yield from stream
        return
    path = Path(source)
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            yield from fh
    except OSError as exc:
        raise InputError(f"无法读取 URL 文件: {source}") from exc


def load_urls(source: str | Path | None = None, *, stdin: TextIO | None = None) -> list[str]:
    """从文件或标准输入加载 URL 列表。

    source 为 None 或 ``"-"`` 时读取 stdin（默认 ``sys.stdin``），否则读取
    ``Path(source)``（``encoding="utf-8-sig"``，兼容 BOM）。空行与 ``#``
    开头的注释行会被跳过，其余行交给 :func:`normalize_input_url` 规范化，
    最后保序去重。

    Args:
        source: URL 文件路径、``"-"``（表示 stdin）或 None（表示 stdin）。
        stdin: 可注入的标准输入流，默认使用 ``sys.stdin``。

    Returns:
        规范化、去重后的 URL 列表。

    Raises:
        InputError: 文件读取失败，或最终未得到任何有效 URL 时抛出。
    """
    collected: list[str] = []
    for line in _iter_source_lines(source, stdin):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        normalized = normalize_input_url(text)
        if normalized is not None:
            collected.append(normalized)
    urls = dedupe(collected)
    if not urls:
        raise InputError("URL 列表为空：未读取到任何有效 URL")
    return urls
