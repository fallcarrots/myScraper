"""extract 模块测试：clean_text / normalize_url / compile_plan / extract_page。

全部使用纯字符串 fixture，不访问网络；断言基于 extract.py 的真实实现行为。
"""

from typing import Final

import pytest
from bs4 import BeautifulSoup

from myscraper.errors import ConfigError, MissingRequired
from myscraper.extract import (
    ExtractResult,
    MultiPlan,
    ScalarPlan,
    SelectorSpec,
    clean_text,
    compile_plan,
    extract_page,
    normalize_url,
)

BASE: Final[str] = "https://example.com/page"


# --- A. clean_text ---


def test_clean_text_removes_zero_width_and_control_keeps_ws() -> None:
    """零宽字符与 \\x07 被删除；\\t \\n 保留后折叠为单空格并 strip。

    删除是"移除"语义而非替换为空格，因此删除后相邻字符自然相连（"a"+"b" → "ab"）。
    """
    out = clean_text("a\u200bb\x07c\td\ne")
    assert out == "abc d e"


def test_clean_text_nbsp_replaced_with_space() -> None:
    """NBSP（\\u00a0）被替换为普通空格。"""
    assert clean_text("a\u00a0b") == "a b"


def test_clean_text_collapses_whitespace_and_strips() -> None:
    """连续空白折叠为单空格并 strip 首尾。"""
    assert clean_text("  a \t\n b  ") == "a b"


def test_clean_text_removes_spaces_between_cjk() -> None:
    """CJK 字符之间的空格被删除。"""
    assert clean_text("中文 测试") == "中文测试"


def test_clean_text_truncates_by_codepoints_no_ellipsis() -> None:
    """按码点截断：'汉'*30 + max_chars=10 → 长度 10，不含省略号。"""
    out = clean_text("汉" * 30, max_chars=10)
    assert len(out) == 10
    assert out == "汉" * 10
    assert "…" not in out
    assert "..." not in out


# --- B. normalize_url ---


def test_normalize_url_resolves_relative() -> None:
    """相对路径基于 base 解析为绝对 URL。"""
    assert normalize_url(BASE, "/a/b") == "https://example.com/a/b"
    assert normalize_url(BASE, "c.html") == "https://example.com/c.html"


def test_normalize_url_drops_fragment() -> None:
    """fragment 被丢弃，query 保留。"""
    assert normalize_url(BASE, "/x?q=1#frag") == "https://example.com/x?q=1"
    assert normalize_url(BASE, "/x#frag") == "https://example.com/x"


def test_normalize_url_drops_non_http_schemes() -> None:
    """javascript: / mailto: / tel: / data: 返回 None。"""
    assert normalize_url(BASE, "javascript:void(0)") is None
    assert normalize_url(BASE, "mailto:a@b.com") is None
    assert normalize_url(BASE, "tel:+123456") is None
    assert normalize_url(BASE, "data:text/plain,hi") is None


def test_normalize_url_rejects_credentials() -> None:
    """netloc 含用户凭据 → None。"""
    assert normalize_url(BASE, "https://u:p@h.example/x") is None


def test_normalize_url_omits_default_port() -> None:
    """http 默认端口 80 / https 443 被省略。"""
    assert normalize_url(BASE, "http://a.example:80/x") == "http://a.example/x"
    assert normalize_url(BASE, "https://a.example:443/y") == "https://a.example/y"


def test_normalize_url_none_or_blank() -> None:
    """None / 空串 / 纯空白 → None。"""
    assert normalize_url(BASE, None) is None
    assert normalize_url(BASE, "") is None
    assert normalize_url(BASE, "   ") is None


# --- C. compile_plan ---


def test_compile_plan_unknown_slot_raises() -> None:
    """未知槽位 → ConfigError。"""
    with pytest.raises(ConfigError):
        compile_plan({"foo": "h1"})


def test_compile_plan_title_all_raises() -> None:
    """title 槽位 all=True → ConfigError。"""
    with pytest.raises(ConfigError):
        compile_plan({"title": {"selector": "h1", "all": True}})


def test_compile_plan_invalid_css_raises() -> None:
    """非法 CSS 选择器 → ConfigError。"""
    with pytest.raises(ConfigError):
        compile_plan({"title": "div[[["})


def test_compile_plan_join_without_all_raises() -> None:
    """标量槽位 all=False 且指定 join → ConfigError。"""
    with pytest.raises(ConfigError):
        compile_plan({"title": {"selector": "h1", "join": " "}})


def test_compile_plan_fragile_selector_strict_raises() -> None:
    """strict_selectors=True 时脆弱选择器（:nth-child）→ ConfigError。"""
    with pytest.raises(ConfigError):
        compile_plan({"title": "li:nth-child(2)"}, strict_selectors=True)


def test_compile_plan_fragile_selector_lenient_ok() -> None:
    """strict_selectors=False 时脆弱选择器正常返回计划（仅告警）。"""
    plans = compile_plan({"title": "li:nth-child(2)"})
    assert isinstance(plans["title"], ScalarPlan)
    assert plans["title"].selector == "li:nth-child(2)"


# --- D. extract_page 默认路径 ---


DEFAULT_DOC: Final[str] = """
<html><head>
<meta property="og:title" content="OG 标题"/>
<title>HTML Title</title>
</head><body>
<main><h1>正文 H1</h1>
<script>var evil = "should not appear";</script>
<style>.x { color: red; }</style>
<p>第一段</p><p>第二段</p>
<a href="/link1">L1</a><a href="/link1">dup</a>
<a href="javascript:void(0)">bad</a>
<a href="rel.html">rel</a>
</main></body></html>
"""


def test_extract_page_default_title_og_wins_over_title() -> None:
    """og:title 优先级高于 <title>。"""
    res = extract_page(DEFAULT_DOC, BASE)
    assert res.title == "OG 标题"
    only_title = "<html><head><title>T</title></head><body></body></html>"
    assert extract_page(only_title, BASE).title == "T"


def test_extract_page_default_text_excludes_script_style() -> None:
    """<script>/<style> 内容不出现在 text 中；容器候选 main 优先生效。"""
    res = extract_page(DEFAULT_DOC, BASE)
    assert res.text != ""
    assert "should not appear" not in res.text
    assert "color" not in res.text
    # get_text(" ") 在相邻元素间插入空格，"正文" 与 "H1" 之间有空格，不能断言连续字符串
    assert "正文" in res.text
    assert "第一段" in res.text


def test_extract_page_default_links_absolute_and_deduped() -> None:
    """相对链接转绝对 URL，且保序去重。"""
    res = extract_page(DEFAULT_DOC, BASE)
    assert res.links == (
        "https://example.com/link1",
        "https://example.com/rel.html",
    )
    assert len(res.links) == len(set(res.links))


def test_extract_page_empty_doc_ok() -> None:
    """空文档不抛异常，返回 ExtractResult 且槽位为空。"""
    res = extract_page("", BASE)
    assert isinstance(res, ExtractResult)
    assert res.title is None
    assert res.text == ""
    assert res.links == ()


# --- E. extract_page 配置路径 ---


H1_DOC: Final[str] = "<html><body><h1>标题一</h1><h1>标题二</h1></body></html>"


def test_extract_page_plan_scalar_title_h1() -> None:
    """compile_plan({'title': 'h1'}) 提取首个 h1 文本。"""
    plan = compile_plan({"title": "h1"})
    res = extract_page(H1_DOC, BASE, plan=plan)
    assert res.title == "标题一"


def test_extract_page_plan_multi_join() -> None:
    """多值槽位 + join=' ' 把多个元素拼成一个字符串。"""
    plan = compile_plan({"body": {"selector": "h1", "all": True, "join": " "}})
    res = extract_page(H1_DOC, BASE, plan=plan)
    assert res.text == "标题一 标题二"
    assert isinstance(plan["body"], MultiPlan)


def test_extract_page_required_miss_raises_and_message_no_angle_bracket() -> None:
    """required=True 未命中 → MissingRequired，且消息不含 '<'。"""
    plan = compile_plan({"title": {"selector": "h1.some", "required": True}})
    with pytest.raises(MissingRequired) as excinfo:
        extract_page(H1_DOC, BASE, plan=plan)
    assert "<" not in str(excinfo.value)


def test_extract_page_links_required_zero_hits_raises() -> None:
    """links 槽位 required=True 且命中 0 条 → MissingRequired。"""
    plan = compile_plan({"links": {"selector": "a[href]", "all": True, "required": True}})
    with pytest.raises(MissingRequired):
        extract_page(H1_DOC, BASE, plan=plan)


def test_selector_spec_defaults() -> None:
    """SelectorSpec 默认值：attr='text'、all=False、join=None、required=False。"""
    spec = SelectorSpec(selector="h1")
    assert spec.attr == "text"
    assert spec.all is False
    assert spec.join is None
    assert spec.required is False


def test_soup_import_unwrapped() -> None:
    """BeautifulSoup 可直接解析 fixture（离线确认解析器可用）。"""
    soup = BeautifulSoup("<p>ok</p>", "html.parser")
    assert soup.get_text() == "ok"
