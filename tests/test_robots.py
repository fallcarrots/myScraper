"""robots 模块测试：纯函数裁决 + RobotsCache 缓存行为（MockTransport，绝不触网）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from fakes import FakeClock

from myscraper.robots import (
    PRODUCT_TOKEN,
    RobotsCache,
    RobotsVerdict,
    build_parser,
    crawl_delay_of,
    evaluate,
)

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ROBOTS_DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private\n"
ROBOTS_TWO_GROUPS = "User-agent: *\nDisallow: /\n\nUser-agent: myscraper\nDisallow: /x\n"
ROBOTS_CRAWL_DELAY_2 = "User-agent: *\nCrawl-delay: 2\nDisallow: /\n"
ROBOTS_CRAWL_DELAY_60 = "User-agent: *\nCrawl-delay: 60\nDisallow: /\n"


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """用 httpx.MockTransport 构造不触网的 AsyncClient。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_counting_client(status: int, body: str) -> tuple[httpx.AsyncClient, list[int]]:
    """构造客户端 + 请求计数器（list[int] 计数，避免 nonlocal 闭包变量）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, text=body)

    return make_client(handler), calls


# ---------- A. 纯函数 ----------


def test_build_parser_then_evaluate_disallowed() -> None:
    """用例 1：Disallow: /private 命中 → evaluate 返回 False。"""
    parser = build_parser(ROBOTS_DISALLOW_PRIVATE)
    assert evaluate(parser, "https://a.example/private/x") is False


def test_build_parser_then_evaluate_allowed() -> None:
    """用例 2：未命中 Disallow 路径 → evaluate 返回 True。"""
    parser = build_parser(ROBOTS_DISALLOW_PRIVATE)
    assert evaluate(parser, "https://a.example/public") is True


def test_product_segment_takes_priority() -> None:
    """用例 3：产品段优先于 * 段；默认 product 即 PRODUCT_TOKEN。"""
    assert PRODUCT_TOKEN == "myscraper"
    parser = build_parser(ROBOTS_TWO_GROUPS)
    assert evaluate(parser, "https://a.example/y") is True
    assert evaluate(parser, "https://a.example/x") is False


def test_crawl_delay_of_parses_and_missing() -> None:
    """用例 4：Crawl-delay: 2 → 2.0（float）；无指令 → None。"""
    parser = build_parser(ROBOTS_CRAWL_DELAY_2)
    delay = crawl_delay_of(parser)
    assert delay == 2.0
    assert isinstance(delay, float)
    assert crawl_delay_of(build_parser(ROBOTS_DISALLOW_PRIVATE)) is None


# ---------- B. RobotsCache 裁决 ----------


async def test_verdict_200_disallow_all() -> None:
    """用例 5：200 + Disallow: / → allowed=False、source='robots'。"""
    client, _ = make_counting_client(200, ROBOTS_DISALLOW_ALL)
    cache = RobotsCache(client, FakeClock())
    async with client:
        verdict = await cache.verdict_for("https://a.example/page")
    assert isinstance(verdict, RobotsVerdict)
    assert verdict.allowed is False
    assert verdict.source == "robots"


async def test_verdict_404_treated_as_absent() -> None:
    """用例 6：404 → RFC 9309 视为无 robots，allowed=True、source='absent'。"""
    client, _ = make_counting_client(404, "")
    cache = RobotsCache(client, FakeClock())
    async with client:
        verdict = await cache.verdict_for("https://a.example/page")
    assert verdict.allowed is True
    assert verdict.source == "absent"


async def test_verdict_500_conservative_skip() -> None:
    """用例 7：500 → 保守整 host 跳过，allowed=False、source='unreachable'。"""
    client, _ = make_counting_client(500, "")
    cache = RobotsCache(client, FakeClock())
    async with client:
        verdict = await cache.verdict_for("https://a.example/page")
    assert verdict.allowed is False
    assert verdict.source == "unreachable"


async def test_verdict_network_error_unreachable() -> None:
    """用例 8：网络异常（ConnectError）→ allowed=False、source='unreachable'。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = make_client(handler)
    cache = RobotsCache(client, FakeClock())
    async with client:
        verdict = await cache.verdict_for("https://a.example/page")
    assert verdict.allowed is False
    assert verdict.source == "unreachable"


async def test_verdict_crawl_delay_over_limit_rejects() -> None:
    """用例 9：Crawl-delay 60 > 上限 30 → allowed=False，不静默缩短。"""
    client, _ = make_counting_client(200, ROBOTS_CRAWL_DELAY_60)
    cache = RobotsCache(client, FakeClock(), max_crawl_delay_s=30.0)
    async with client:
        verdict = await cache.verdict_for("https://a.example/page")
    assert verdict.allowed is False
    assert verdict.crawl_delay_s == 60.0
    assert verdict.source == "robots"


async def test_verdict_invalid_url_raises_value_error() -> None:
    """用例 10：非法 URL（缺 scheme/host）→ ValueError。"""
    client, _ = make_counting_client(200, ROBOTS_DISALLOW_PRIVATE)
    cache = RobotsCache(client, FakeClock())
    async with client:
        with pytest.raises(ValueError, match="robots"):
            await cache.verdict_for("not-a-url")


# ---------- C. 缓存行为 ----------


async def test_cache_concurrent_single_fetch() -> None:
    """用例 11：同 origin 并发 20 次 → robots.txt 只拉取 1 次（防击穿）。"""
    client, calls = make_counting_client(200, ROBOTS_DISALLOW_PRIVATE)
    cache = RobotsCache(client, FakeClock())
    url = "https://a.example/page"
    async with client:
        verdicts = await asyncio.gather(*[cache.verdict_for(url) for _ in range(20)])
    assert len(calls) == 1
    assert all(v.source == "robots" for v in verdicts)


async def test_cache_ttl_expiry_refetch() -> None:
    """用例 12：ttl_s=10，clock.advance(11.0) 后重取 → 共拉取 2 次。"""
    client, calls = make_counting_client(200, ROBOTS_DISALLOW_PRIVATE)
    clock = FakeClock()
    cache = RobotsCache(client, clock, ttl_s=10.0)
    url = "https://a.example/page"
    async with client:
        await cache.verdict_for(url)
        clock.advance(11.0)
        await cache.verdict_for(url)
    assert len(calls) == 2


async def test_cache_distinct_origins_fetched_separately() -> None:
    """用例 13：两个不同 origin 各自拉取 → 共 2 次，互不共享缓存。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text=ROBOTS_DISALLOW_PRIVATE)

    client = make_client(handler)
    cache = RobotsCache(client, FakeClock())
    async with client:
        first = await cache.verdict_for("https://a.example/p")
        second = await cache.verdict_for("https://b.example/p")
    assert len(calls) == 2
    assert first.source == "robots"
    assert second.source == "robots"
