"""解析层：HTML → 结构化抽取结果的纯计算模块。

模块定位（合规与架构红线）：
1. 本模块是**纯计算**层——输入 `(html_text, base_url)`，输出 `ExtractResult`；
   **禁止任何网络 I/O**，不读文件、不写文件。
2. 输出**不含任何 PII 字段**，仅有 title / text / links 三个槽位。
3. 解析器固定使用 Python 内置 `"html.parser"`，**禁止 lxml**，避免额外二进制依赖。
"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import soupsieve
from bs4 import BeautifulSoup
from bs4.element import Tag
from loguru import logger

from .errors import ConfigError, MissingRequired

__all__ = [
    "DROP_SCHEMES",
    "HTML_PARSER",
    "SKIP_TAGS",
    "SLOTS",
    "ExtractResult",
    "MultiPlan",
    "ScalarPlan",
    "SelectorSpec",
    "clean_text",
    "compile_plan",
    "extract_page",
    "normalize_url",
]

#: 解析正文前需要整体移除的标签（脚本、样式、交互组件等，均不贡献正文文本）。
SKIP_TAGS: Final[frozenset[str]] = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "canvas", "form", "button"}
)
#: 丢弃的 URL scheme（非 http/https，或无法构成可请求链接）。
DROP_SCHEMES: Final[frozenset[str]] = frozenset(
    {"javascript", "mailto", "tel", "data", "blob", "filesystem"}
)
#: 合法的选择器槽位：title / body(text) / links。
SLOTS: Final[tuple[str, ...]] = ("title", "body", "links")
#: 固定使用的 HTML 解析器（内置，禁止 lxml）。
HTML_PARSER: Final[str] = "html.parser"

#: CJK 与全角标点字符区间（中日韩统一表意文字、兼容表意、假名、谚文等）。
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
#: 删除的零宽与控制字符（保留 \t \n）。
_ZW_CTRL = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
#: 各种 Unicode 空白（含 NBSP 系）折叠为普通空格前，先统一替换的字符集。
_NBSP = "\u00a0\u2007\u202f"
#: 脆弱选择器模式：命中时 strict 模式报错、宽松模式仅告警。
_FRAGILE_RE = re.compile(r":nth-child|:nth-of-type|:has\(")
#: CJK 字符之间的空白删除模式。
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
#: 任意连续空白（含换行）折叠为单空格的模式。
_WS_RE = re.compile(r"\s+")

#: 文本清理后用于截断的默认上限。
_DEFAULT_MAX_CHARS: Final[int] = 20000


def clean_text(raw: str, *, max_chars: int = 20000) -> str:
    """清理 HTML 抽取出的原始文本。

    处理顺序：删除零宽字符与控制字符（保留 \\t \\n）→ NBSP 系列替换为普通空格 →
    连续空白折叠为单空格并 strip → 删除 CJK 字符之间的空格 → 按码点截断。

    按码点（而非字节）截断，CJK/emoji 不会被切断；截断时不插入任何标记
    （不加省略号），保证输出是干净的纯文本。

    Args:
        raw: 待清理的原始文本。
        max_chars: 截断上限（按 Python 码点数，非字节数）。

    Returns:
        清理并截断后的文本；输入为空时返回空字符串。
    """
    if not raw:
        return ""
    # 1. 删除零宽字符与控制字符（保留 \t \n）
    zw_table: Final[dict[int, str | None]] = {ord(c): None for c in _ZW_CTRL}
    text = raw.translate(zw_table)
    text = "".join(
        ch for ch in text if ch in "\t\n" or not unicodedata.category(ch).startswith("C")
    )
    # 2. NBSP 系列替换为普通空格
    nbsp_table: Final[dict[int, str | None]] = {ord(c): " " for c in _NBSP}
    text = text.translate(nbsp_table)
    # 3. 连续空白折叠为单空格并 strip
    text = _WS_RE.sub(" ", text).strip()
    # 4. 删除 CJK 字符之间的空格
    text = _CJK_SPACE_RE.sub("", text)
    # 5. 按码点截断（不插入任何标记）
    return text[:max_chars]


def normalize_url(base_url: str, href: str | None) -> str | None:
    """将相对/绝对 href 规范化为绝对 http(s) URL。

    规则：None/空白 → None；无 scheme 时基于 base_url 做 urljoin；
    scheme 必须 http/https 且不在 DROP_SCHEMES；netloc 含 @（凭据）或无
    hostname → None；默认端口省略；path 为空补 "/"；丢弃 fragment；query 保留。

    Args:
        base_url: 页面基准 URL，用于解析相对链接。
        href: 原始 href 属性值。

    Returns:
        规范化后的绝对 URL；无法规范化时返回 None。
    """
    if href is None:
        return None
    href = href.strip()
    if not href:
        return None
    try:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href):
            candidate = href
        else:
            candidate = urljoin(base_url, href)
        parts = urlsplit(candidate)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or scheme in DROP_SCHEMES:
            return None
        netloc = parts.netloc
        if "@" in netloc:  # 含凭据
            return None
        if not parts.hostname:
            return None
        port = parts.port  # 无端口时为 None；非法端口抛 ValueError
        if (scheme, port) == ("http", 80) or (scheme, port) == ("https", 443):
            netloc = parts.hostname
            if parts.username or parts.password:
                return None
            # hostname 不含端口；此处 netloc 已排除凭据（上文 @ 检查）
        path = parts.path or "/"
        return urlunsplit((scheme, netloc, path, parts.query, ""))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    """单个槽位的选择器声明（尚未编译）。

    Attributes:
        selector: CSS 选择器表达式。
        attr: 取值来源：元素文本 / href / src / html（str(tag)）。
        all: 是否取所有匹配（列表）；False 表示标量（首个匹配）。
        join: 多值连接符；仅在 all=True 时有效，None 表示不 join。
        required: 未命中时是否抛 MissingRequired。
    """

    selector: str
    attr: Literal["text", "href", "src", "html"] = "text"
    all: bool = False
    join: str | None = None
    required: bool = False


@dataclass(frozen=True, slots=True)
class ScalarPlan:
    """已编译的标量槽位计划（取首个匹配）。

    Attributes:
        slot: 语义槽位名（title / body / links 之一）。
        selector: 原始 CSS 选择器表达式。
        compiled: soupsieve 预编译后的选择器。
        attr: 取值来源（text / href / src / html）。
        required: 未命中时是否抛 MissingRequired。
    """

    slot: str
    selector: str
    compiled: soupsieve.SoupSieve
    attr: str
    required: bool


@dataclass(frozen=True, slots=True)
class MultiPlan:
    """已编译的列表槽位计划（取所有匹配）。

    Attributes:
        slot: 语义槽位名。
        selector: 原始 CSS 选择器表达式。
        compiled: soupsieve 预编译后的选择器。
        attr: 取值来源（text / href / src / html）。
        join: 多值连接符；None 表示保持列表。
        max_items: 列表截断上限。
        required: 未命中时是否抛 MissingRequired。
    """

    slot: str
    selector: str
    compiled: soupsieve.SoupSieve
    attr: str
    join: str | None
    max_items: int
    required: bool = False


#: 槽位名 → 编译后计划的映射类型。
Plans = Mapping[str, ScalarPlan | MultiPlan]


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """extract_page 的输出：title / text / links 三槽位，不含任何 PII 字段。

    Attributes:
        title: 页面标题；未命中时为 None。
        text: 正文纯文本（已清理截断）；未命中时为空字符串。
        links: 规范化去重后的绝对链接；未命中时为空元组。
    """

    title: str | None
    text: str
    links: tuple[str, ...]


def _spec_from_value(slot: str, value: object) -> SelectorSpec:
    """把 plan 配置值（str 或 Mapping）转换为 SelectorSpec。

    Args:
        slot: 槽位名（用于错误消息）。
        value: 配置值：str 视为纯选择器；Mapping 视为 SelectorSpec 关键字参数。

    Returns:
        对应的 SelectorSpec。

    Raises:
        ConfigError: 值类型非法或 Mapping 含未知键。
    """
    if isinstance(value, str):
        return SelectorSpec(selector=value)
    if isinstance(value, Mapping):
        valid = set(SelectorSpec.__dataclass_fields__)
        unknown = set(value.keys()) - valid
        if unknown:
            keys = ", ".join(sorted(unknown))
            raise ConfigError(f"槽位 {slot} 含未知配置键: {keys}")
        return SelectorSpec(**value)
    raise ConfigError(
        f"槽位 {slot} 的选择器配置必须是 str 或 Mapping，实际为 {type(value).__name__}"
    )


def compile_plan(
    raw: Mapping[str, object], *, strict_selectors: bool = False, max_links: int = 500
) -> dict[str, ScalarPlan | MultiPlan]:
    """把原始选择器配置编译为预编译的抽取计划。

    配置形如::

        {"title": "h1",
         "body": {"selector": "article", "attr": "text"},
         "links": {"selector": "a[href]", "all": True, "attr": "href"}}

    基数规则：title 必须标量（all=True 报错）；links 天然是列表，all=False 时
    强制视为 True；任何槽位 all=False 且 join 非空 → 报错。

    Args:
        raw: 槽位名 → 选择器配置（str 或关键字 Mapping）。
        strict_selectors: True 时脆弱选择器（:nth-child 等）直接报错；
            False 时仅记录 WARNING。
        max_links: links 槽位的截断上限。

    Returns:
        槽位名 → ScalarPlan / MultiPlan 的映射。

    Raises:
        ConfigError: 未知槽位、未知配置键、基数违规、非法 CSS 选择器，
            或 strict 模式下的脆弱选择器。
    """
    plans: dict[str, ScalarPlan | MultiPlan] = {}
    for slot, value in raw.items():
        if slot not in SLOTS:
            raise ConfigError(f"未知的选择器槽位: {slot}")
        spec = _spec_from_value(slot, value)
        # 基数校验
        if slot == "title" and spec.all:
            raise ConfigError("title 槽位必须是标量，不支持 all=True")
        is_all = spec.all or slot == "links"  # links 天然是列表
        if not is_all and spec.join is not None:
            raise ConfigError("join 仅在 all=True 时有效")
        # 预编译选择器
        try:
            compiled = soupsieve.compile(spec.selector)
        except soupsieve.SelectorSyntaxError as exc:
            raise ConfigError(f"非法 CSS 选择器: {spec.selector}") from exc
        # 脆弱选择器检测
        if _FRAGILE_RE.search(spec.selector):
            if strict_selectors:
                raise ConfigError(f"脆弱选择器（依赖 DOM 位置，易因模板变更失效）: {spec.selector}")
            logger.warning(
                "脆弱选择器（依赖 DOM 位置，易因模板变更失效）: 槽位={} selector={}",
                slot,
                spec.selector,
            )
        if is_all:
            plans[slot] = MultiPlan(
                slot=slot,
                selector=spec.selector,
                compiled=compiled,
                attr=spec.attr,
                join=spec.join,
                max_items=max_links if slot == "links" else _DEFAULT_MAX_CHARS,
                required=spec.required,
            )
        else:
            plans[slot] = ScalarPlan(
                slot=slot,
                selector=spec.selector,
                compiled=compiled,
                attr=spec.attr,
                required=spec.required,
            )
    return plans


def _default_title(soup: BeautifulSoup) -> str | None:
    """按默认候选链抽取标题：og:title → <title> → <h1> → None。

    Args:
        soup: 已解析的文档树。

    Returns:
        首个命中的标题文本（已 clean_text）；全部未命中时为 None。
    """
    candidates: tuple[str, ...] = (
        'meta[property="og:title"][content]',
        "title",
        "h1",
    )
    for css in candidates:
        tag = soup.select_one(css)
        if tag is None:
            continue
        if css.startswith("meta"):
            content = tag.get("content")
            if isinstance(content, str):
                text = clean_text(content)
                if text:
                    return text
            continue
        text = clean_text(tag.get_text(" "))
        if text:
            return text
    return None


def _default_text(soup: BeautifulSoup, *, max_chars: int) -> str:
    """按默认候选链抽取正文：main → article → body → 整文档。

    先对 find_all(SKIP_TAGS) 取快照再逐个 decompose()（禁止边遍历边改树），
    然后 get_text(" ") 并过 clean_text。

    Args:
        soup: 已解析的文档树（会被就地修改：移除 SKIP_TAGS）。
        max_chars: 文本截断上限。

    Returns:
        清理后的正文文本；无内容时为空字符串。
    """
    containers = (
        soup.find("main"),
        soup.find("article"),
        soup.find("body"),
    )
    target: Tag | BeautifulSoup = next((t for t in containers if t is not None), soup)
    for tag in target.find_all(SKIP_TAGS):  # find_all 返回快照列表，可安全逐个删除
        tag.decompose()
    return clean_text(target.get_text(" "), max_chars=max_chars)


def _default_links(soup: BeautifulSoup, base_url: str, *, max_links: int) -> tuple[str, ...]:
    """抽取页面全部 a[href] 并规范化、保序去重、截断。

    Args:
        soup: 已解析的文档树。
        base_url: 用于解析相对链接的基准 URL。
        max_links: 截断上限。

    Returns:
        规范化绝对链接元组（保序去重后）。
    """
    seen: set[str] = set()
    result: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        url = normalize_url(base_url, href if isinstance(href, str) else None)
        if url is None or url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= max_links:
            break
    return tuple(result)


def _attr_value(tag: Tag, attr: str) -> str | None:
    """从单个标签按 attr 声明取原始值。

    Args:
        tag: 命中的标签。
        attr: 取值来源：text / href / src / html。

    Returns:
        原始值；属性缺失时为 None。html 恒有值（str(tag)）。
    """
    if attr == "text":
        return tag.get_text(" ")
    if attr == "html":
        return str(tag)
    value = tag.get(attr)
    if isinstance(value, list):  # 多值属性（如 class）取首值
        return value[0] if value else None
    return value


def _extract_slot_value(
    soup: BeautifulSoup, base_url: str, plan: ScalarPlan | MultiPlan, *, max_chars: int
) -> tuple[str | None, list[str]]:
    """按单个已编译计划抽取一个槽位的值。

    Args:
        soup: 已解析的文档树。
        base_url: href / src 属性相对链接的基准 URL。
        plan: 标量或列表计划。
        max_chars: text / html 取值的清理截断上限。

    Returns:
        (标量值或 None, 列表值)；标量计划列表恒为空，列表计划标量恒为 None。
    """
    if isinstance(plan, ScalarPlan):
        tag = plan.compiled.select_one(soup)
        if tag is None:
            return None, []
        raw = _attr_value(tag, plan.attr)
        if raw is None:
            return None, []
        if plan.attr in ("href", "src"):
            normalized = normalize_url(base_url, raw)
            return normalized, []
        return clean_text(raw, max_chars=max_chars), []

    tags = plan.compiled.select(soup)[: plan.max_items]
    values: list[str] = []
    for tag in tags:
        raw = _attr_value(tag, plan.attr)
        if raw is None:
            continue
        if plan.attr in ("href", "src"):
            normalized = normalize_url(base_url, raw)
            if normalized is not None:
                values.append(normalized)
        else:
            cleaned = clean_text(raw, max_chars=max_chars)
            if cleaned:
                values.append(cleaned)
    return None, values


def extract_page(
    html_text: str,
    base_url: str,
    *,
    plan: Plans | None = None,
    max_text_chars: int = 20000,
    max_links: int = 500,
) -> ExtractResult:
    """从 HTML 文本抽取 title / text / links（纯计算，无任何网络 I/O）。

    plan 为 None 或缺某槽位时走默认抽取：
      - title：og:title → <title> → <h1> → None
      - text：main → article → body → 整文档（移除 SKIP_TAGS 后取文本）
      - links：a[href] → normalize_url → 保序去重 → 截断 max_links

    配置抽取：标量槽位 select_one，列表槽位 select 且截断 max_items；
    attr="html" 取 str(tag)（过 clean_text）；join 非空时把多值 join 成字符串。

    Args:
        html_text: 原始 HTML 文本。
        base_url: 基准 URL（解析相对链接）。
        plan: 预编译抽取计划；None 或缺槽位时使用默认抽取。
        max_text_chars: 正文文本截断上限。
        max_links: 链接数截断上限。

    Returns:
        ExtractResult(title, text, links)。

    Raises:
        MissingRequired: required=True 的槽位未命中。
        RecursionError: HTML 嵌套过深导致解析栈溢出（由 runner 转 failed）。
        ValueError: 极端畸形文档等解析器异常（由 runner 转 failed）。
    """
    soup = BeautifulSoup(html_text, HTML_PARSER)

    title: str | None
    text: str
    links: tuple[str, ...]

    title_plan = plan.get("title") if plan is not None else None
    if title_plan is not None:
        scalar, _ = _extract_slot_value(soup, base_url, title_plan, max_chars=max_text_chars)
        if scalar is None:
            if title_plan.required:
                raise MissingRequired(slot="title", selector=title_plan.selector)
            title = None
        else:
            title = scalar
    else:
        title = _default_title(soup)

    body_plan = plan.get("body") if plan is not None else None
    if body_plan is not None:
        scalar, values = _extract_slot_value(soup, base_url, body_plan, max_chars=max_text_chars)
        if isinstance(body_plan, MultiPlan) and body_plan.join is not None:
            text = body_plan.join.join(values) if values else ""
            if not text and body_plan.required:
                raise MissingRequired(slot="body", selector=body_plan.selector)
        elif scalar is not None:
            text = scalar
        elif isinstance(body_plan, MultiPlan):
            text = "\n".join(values)
        else:
            text = ""
        if body_plan.required and not text:
            raise MissingRequired(slot="body", selector=body_plan.selector)
    else:
        text = _default_text(soup, max_chars=max_text_chars)

    links_plan = plan.get("links") if plan is not None else None
    if links_plan is not None:
        _, values = _extract_slot_value(soup, base_url, links_plan, max_chars=max_text_chars)
        links = tuple(values)[:max_links]
        if not links and links_plan.required:
            raise MissingRequired(slot="links", selector=links_plan.selector)
    else:
        links = _default_links(soup, base_url, max_links=max_links)

    return ExtractResult(title=title, text=text, links=links)
