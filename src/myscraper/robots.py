"""robots.txt 合规判定：拉取、解析、进程内缓存、按 URL 给出裁决。

本模块是 robots.txt 合规的唯一实现点，职责链为：
    拉取（httpx，受超时/限速管辖） -> 解析（urllib.robotparser，纯内存）
    -> 进程内缓存（按 origin 共享 Task，防击穿） -> 按 URL 给出 `RobotsVerdict`。

**合规红线**：robots 检查默认生效且本模块不提供任何关闭入口；
无法确认 robots.txt 状态时一律保守判为不可抓（`source="unreachable"`），
不存在静默放行的代码路径。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .clock import Clock

__all__ = [
    "MAX_ROBOTS_BYTES",
    "PRODUCT_TOKEN",
    "ROBOTS_PATH",
    "RobotsCache",
    "RobotsVerdict",
    "VerdictSource",
    "build_parser",
    "crawl_delay_of",
    "evaluate",
]

#: can_fetch / crawl_delay 必须用的产品 token（不是完整 UA 串）。
PRODUCT_TOKEN: Final[str] = "myscraper"  # noqa: S105 —— UA 产品标识，非凭据
#: 每个 origin 的 robots.txt 标准路径。
ROBOTS_PATH: Final[str] = "/robots.txt"
#: robots.txt 解析上限（RFC 9309 §2.2.1 要求至少支持 500 KiB）。
MAX_ROBOTS_BYTES: Final[int] = 512 * 1024

#: 裁决来源：
#: - ``robots``：成功拉取并解析了 robots.txt；
#: - ``absent``：4xx（含 404），按 RFC 9309 视为允许；
#: - ``unreachable``：5xx 或网络错误，保守判为整 host 不可抓。
VerdictSource = Literal["robots", "absent", "unreachable"]


@dataclass(frozen=True, slots=True)
class RobotsVerdict:
    """一次 robots 判定的不可变结果。

    Attributes:
        allowed: 该 URL 是否允许抓取。
        crawl_delay_s: 对应产品段的 Crawl-delay（秒）；不可用时为 None。
        source: 裁决依据来源，见 `VerdictSource`。
    """

    allowed: bool
    crawl_delay_s: float | None
    source: VerdictSource


@dataclass(frozen=True, slots=True)
class _OriginRules:
    """某个 origin 的 robots 元信息（不含 URL 级裁决，仅内部使用）。

    Attributes:
        parser: 解析后的 robots 规则；None 表示无 robots.txt（absent/unreachable）。
        source: 规则来源，见 `VerdictSource`。
        crawl_delay_s: 对应产品段的 Crawl-delay（秒）；不可用时为 None。
        blocked: True 表示整 host 跳过（5xx/网络错误/crawl-delay 过大）。
    """

    parser: RobotFileParser | None
    source: VerdictSource
    crawl_delay_s: float | None
    blocked: bool


def build_parser(text: str) -> RobotFileParser:
    """把 robots.txt 文本解析为 `RobotFileParser`（纯函数，无 I/O）。

    刻意不用 `RobotFileParser.read()`：那是同步 urllib 网络请求，
    不受项目超时/限速管辖；网络 I/O 一律交给注入的 httpx 客户端。
    """
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser


def evaluate(parser: RobotFileParser, url: str, *, product: str = PRODUCT_TOKEN) -> bool:
    """判定 url 是否允许抓取（纯函数，无 I/O）。

    必须传产品 token（默认 `PRODUCT_TOKEN`），而非完整 User-Agent 串，
    否则 robots.txt 中 ``User-agent: myscraper`` 分段匹配不上。
    """
    return parser.can_fetch(product, url)


def crawl_delay_of(parser: RobotFileParser, *, product: str = PRODUCT_TOKEN) -> float | None:
    """取产品段 Crawl-delay（秒）；未声明或不可解析时返回 None（纯函数，无 I/O）。"""
    raw = parser.crawl_delay(product)
    return None if raw is None else float(raw)


class RobotsCache:
    """按 origin 缓存 robots 规则（parser），按 URL 实时计算裁决。

    缓存键是 origin（``scheme://host:port``，netloc 保持原样，不补默认端口），
    值为 ``(asyncio.Task[_OriginRules], 写入时刻 monotonic)``。

    **防缓存击穿**：`verdict_for` 先创建拉取 Task 存入字典，再 ``await`` 它。
    同一 origin 的并发调用因此共享同一个 Task，天然只发起一次网络请求；
    不会出现"N 个并发请求各自拉一次 robots.txt"的重复拉取。

    **URL 级裁决不缓存**：缓存的只是 origin 级 parser 与元信息；
    `allowed` 每次按本次 URL 实时用 `evaluate` 计算，确保 ``Disallow: /private``
    等路径级规则对每个 URL 独立生效，绝不因命中缓存而放行。

    **TTL**：仅当命中缓存且 ``clock.monotonic() - stored_at < ttl_s`` 才复用旧
    Task；过期后重建 Task 并替换旧条目。缓存不设上限（单次运行规模有限），
    且只存在于内存，不落盘。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        *,
        ttl_s: float = 3600.0,
        max_crawl_delay_s: float = 30.0,
    ) -> None:
        """初始化缓存。

        Args:
            client: 用于拉取 robots.txt 的 httpx 异步客户端（超时/限速由调用方管辖）。
            clock: 注入的时钟，用于 TTL 判定。
            ttl_s: 缓存条目存活秒数。
            max_crawl_delay_s: 可接受的 Crawl-delay 上限；超出即判为不可抓。
        """
        self._client = client
        self._clock = clock
        self._ttl_s = ttl_s
        self._max_crawl_delay_s = max_crawl_delay_s
        self._entries: dict[str, tuple[asyncio.Task[_OriginRules], float]] = {}

    async def verdict_for(self, url: str) -> RobotsVerdict:
        """给出 url 的 robots 裁决；origin 级规则缓存 + URL 级实时判定。

        Raises:
            ValueError: url 缺少 scheme 或 host（由调用方转为 failed 记录）。
        """
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            msg = f"url 缺少 scheme 或 host，无法判定 robots: {url!r}"
            raise ValueError(msg)
        origin = f"{parts.scheme}://{parts.netloc}"
        now = self._clock.monotonic()

        cached = self._entries.get(origin)
        if cached is not None and now - cached[1] < self._ttl_s:
            rules = await cached[0]
        else:
            # 先建 Task 并占位，再 await：并发同 origin 调用共享同一 Task，防击穿。
            task = asyncio.create_task(self._fetch_rules(origin))
            self._entries[origin] = (task, now)
            rules = await task

        if rules.blocked:
            return RobotsVerdict(
                allowed=False, crawl_delay_s=rules.crawl_delay_s, source=rules.source
            )
        if rules.parser is None:
            return RobotsVerdict(
                allowed=True, crawl_delay_s=rules.crawl_delay_s, source=rules.source
            )
        # 裁决按本次 URL 实时计算，不缓存 URL 级结果。
        allowed = evaluate(rules.parser, url)
        return RobotsVerdict(
            allowed=allowed, crawl_delay_s=rules.crawl_delay_s, source=rules.source
        )

    async def _fetch_rules(self, origin: str) -> _OriginRules:
        """拉取并解析 origin 的 robots.txt，产出 origin 级规则（含保守降级）。

        流式读取并以 `MAX_ROBOTS_BYTES` 截断，防止超大 robots.txt 全量缓冲导致 OOM。
        """
        # 局部导入以避免 http↔robots 循环依赖（http.py 顶部 import 了 RobotsCache）。
        from .http import read_capped

        try:
            request = self._client.build_request("GET", origin + ROBOTS_PATH)
            response = await self._client.send(request, stream=True)
            try:
                status = response.status_code
                if status == 200:
                    text, _ = await read_capped(response, MAX_ROBOTS_BYTES)
                else:
                    text = ""
            finally:
                await response.aclose()
        except (httpx.HTTPError, httpx.InvalidURL):
            # 保守：拉不到 robots.txt 就整 host 跳过，绝不静默放行。
            # InvalidURL 不继承 HTTPError（构造 Request 时同步抛出），须显式捕获。
            return _OriginRules(parser=None, source="unreachable", crawl_delay_s=None, blocked=True)

        if status == 200:
            parser = build_parser(text)
            delay = crawl_delay_of(parser)
            if delay is not None and delay > self._max_crawl_delay_s:
                return _OriginRules(
                    parser=parser, source="robots", crawl_delay_s=delay, blocked=True
                )
            return _OriginRules(parser=parser, source="robots", crawl_delay_s=delay, blocked=False)
        if 400 <= status < 500:
            # RFC 9309：4xx（含 404）视为无 robots.txt 限制，允许抓取。
            return _OriginRules(parser=None, source="absent", crawl_delay_s=None, blocked=False)
        # 5xx 及其他状态码：保守判为不可抓。
        return _OriginRules(parser=None, source="unreachable", crawl_delay_s=None, blocked=True)
