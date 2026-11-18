"""http 模块测试：HttpFetcher 编排 / build_client / 合规红线（MockTransport，零真实网络）。

时间一律注入 FakeClock（sleep 只记录不真睡），网络一律走 httpx.MockTransport，
绝不发起真实请求；robots 复核与目标抓取共用同一个 MockTransport 客户端。
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import httpx
from fakes import FakeClock

from myscraper.config import AppConfig, HttpSettings, default_config
from myscraper.http import HttpFetcher, build_client
from myscraper.ratelimit import HostRateLimiter
from myscraper.retry import RetryPolicy
from myscraper.robots import RobotsCache

TARGET_URL: str = "https://a.example/page"
REDIRECT_URL: str = "https://a.example/private/x"
PAGE_TEXT: str = "<html><body>hello</body></html>"
ROBOTS_DISALLOW_ALL: str = "User-agent: *\nDisallow: /\n"
ROBOTS_DISALLOW_PRIVATE: str = "User-agent: *\nDisallow: /private\n"
ROBOTS_ALLOW_ALL: str = "User-agent: *\nDisallow:\n"
USER_AGENT: str = "myscraper/0.1.0 (+https://example.com)"


def _make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 3,
    rate_limit_per_host: float = 10.0,
    min_request_interval: float = 0.0,
    max_html_bytes: int = 5242880,
) -> tuple[HttpFetcher, httpx.AsyncClient, FakeClock]:
    """用 MockTransport 客户端 + FakeClock 组装 HttpFetcher（robots 共用同一客户端）。"""
    clock = FakeClock()
    policy = RetryPolicy(max_retries=max_retries, base_backoff_s=0.5, max_backoff_s=30.0)
    # follow_redirects=True 与 build_client 行为对齐（重定向由客户端跟随）。
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True, max_redirects=5
    )
    robots = RobotsCache(client, clock)
    limiter = HostRateLimiter(
        clock,
        rate_limit_per_host=rate_limit_per_host,
        min_request_interval=min_request_interval,
    )
    fetcher = HttpFetcher(
        client,
        limiter,
        policy,
        robots,
        clock,
        rate_limit_per_host=rate_limit_per_host,
        min_request_interval=min_request_interval,
        max_html_bytes=max_html_bytes,
    )
    return fetcher, client, clock


def _robots_or_page(
    handler_body: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """包装 handler：/robots.txt 统一放行，其余路径交给给定逻辑。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return handler_body(request)

    return handler


# ---------- 1. 200 正常 ----------


async def test_ok_200_returns_text_and_final_url() -> None:
    """用例 1：200 → status='ok'、text/final_url/status_code 正确。"""
    calls: list[httpx.Request] = []

    def body(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, _ = _make_fetcher(_robots_or_page(body))
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "ok"
    assert outcome.text == PAGE_TEXT
    assert outcome.final_url == TARGET_URL
    assert outcome.status_code == 200
    assert outcome.attempts == 1
    assert outcome.truncated is False
    assert len(calls) == 1


# ---------- 2. 体积上限 ----------


async def test_body_over_limit_truncated() -> None:
    """用例 2：body 110 字节 > max_html_bytes=100 → truncated=True 且 len(text.encode())<=100。"""
    body = "x" * 110
    cfg: AppConfig = default_config().model_copy(update={"http": HttpSettings(max_html_bytes=100)})

    def body_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    fetcher, client, _ = _make_fetcher(
        _robots_or_page(body_handler), max_html_bytes=cfg.http.max_html_bytes
    )
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "ok"
    assert outcome.truncated is True
    assert len(outcome.text.encode()) <= 100


# ---------- 3. 500 后成功 ----------


async def test_500_then_200_recovers() -> None:
    """用例 3：第一次 500、第二次 200 → status='ok'、attempts=2。"""
    state: dict[str, int] = {"count": 0}

    def body(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, _ = _make_fetcher(_robots_or_page(body))
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "ok"
    assert outcome.attempts == 2
    assert state["count"] == 2


# ---------- 4. 重试耗尽 ----------


async def test_500_exhausts_retries() -> None:
    """用例 4：始终 500、max_retries=3 → status='failed'、error='http/500'、attempts=4。"""
    calls: list[int] = []

    def body(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    fetcher, client, _ = _make_fetcher(_robots_or_page(body), max_retries=3)
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "failed"
    assert outcome.error == "http/500"
    assert outcome.attempts == 4
    assert len(calls) == 4


# ---------- 5. 403 不重试 ----------


async def test_403_not_retried() -> None:
    """用例 5：403 禁止类错误不重试 → status='failed'、attempts=1、handler 只调用 1 次。"""
    calls: list[int] = []

    def body(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403)

    fetcher, client, _ = _make_fetcher(_robots_or_page(body))
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "failed"
    assert outcome.error == "http/403"
    assert outcome.attempts == 1
    assert len(calls) == 1


# ---------- 6. 429 尊重 Retry-After ----------


async def test_429_respects_retry_after() -> None:
    """用例 6：429 + Retry-After: 5 → 第二次成功，FakeClock.sleeps 含 5.0 且仅此一条。"""
    state: dict[str, int] = {"count": 0}

    def body(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, clock = _make_fetcher(_robots_or_page(body))
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "ok"
    assert outcome.attempts == 2
    assert 5.0 in clock.sleeps
    # 虚拟时间已被 Retry-After 推进 5s，超过限速间隔，第二次 acquire 不再额外 sleep。
    assert len(clock.sleeps) == 1


# ---------- 7. 网络异常重试 ----------


async def test_network_error_retries_then_success() -> None:
    """用例 7：抛 httpx.ConnectError 两次后成功 → status='ok'、attempts=3。"""
    state: dict[str, int] = {"count": 0}

    def body(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] <= 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, _ = _make_fetcher(_robots_or_page(body))
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "ok"
    assert outcome.attempts == 3


# ---------- 8. robots 禁止 ----------


async def test_robots_disallow_skips_without_target_request() -> None:
    """用例 8：Disallow: / → skipped/robots/robots，目标 URL 一次都没被请求。"""
    target_calls: list[int] = []
    all_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        all_calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_DISALLOW_ALL)
        target_calls.append(1)
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, _ = _make_fetcher(handler)
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "skipped"
    assert outcome.skip_reason == "robots/robots"
    assert target_calls == []
    assert len(all_calls) == 1  # 仅 robots.txt 被请求。


# ---------- 9. robots 5xx ----------


async def test_robots_5xx_unreachable_skips() -> None:
    """用例 9：/robots.txt 返回 500 → skipped/robots/unreachable，目标未被请求。"""
    target_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        target_calls.append(1)
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, _ = _make_fetcher(handler)
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "skipped"
    assert outcome.skip_reason == "robots/unreachable"
    assert target_calls == []


# ---------- 10. 重定向到被禁路径 ----------


async def test_redirect_to_disallowed_path_skips() -> None:
    """用例 10：目标 200 但重定向终点落在 Disallow /private → skipped/redirect_disallowed。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_DISALLOW_PRIVATE)
        if request.url.path == "/page":
            return httpx.Response(302, headers={"location": REDIRECT_URL})
        return httpx.Response(200, text="<html>private</html>")

    fetcher, client, _ = _make_fetcher(handler)
    async with client:
        outcome = await fetcher.fetch(TARGET_URL)
    assert outcome.status == "skipped"
    assert outcome.skip_reason == "robots/redirect_disallowed"
    assert outcome.final_url == REDIRECT_URL
    assert outcome.status_code == 200


# ---------- 11. 限速生效 ----------


async def test_rate_limit_first_acquire_no_sleep_second_is_interval() -> None:
    """用例 11：rate_limit_per_host=1.0、min_request_interval=0.5 → 首次 acquire 不 sleep；
    第二次抓取 sleep 恰为 compute_interval 的 1.0 秒。"""

    def body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PAGE_TEXT)

    fetcher, client, clock = _make_fetcher(
        _robots_or_page(body), rate_limit_per_host=1.0, min_request_interval=0.5
    )
    async with client:
        first = await fetcher.fetch(TARGET_URL)
        assert clock.sleeps == []  # 首次 acquire 不 sleep（至多 1 条，实际 0 条）。
        second = await fetcher.fetch(TARGET_URL)
    assert first.status == "ok"
    assert second.status == "ok"
    assert len(clock.sleeps) <= 1
    assert clock.sleeps == [1.0]


# ---------- 12. UA 注入 ----------


async def test_user_agent_injected_and_not_mozilla() -> None:
    """用例 12：build_client 注入产品 UA，且不含 Mozilla（零网络，只查客户端配置）。"""
    ua = USER_AGENT
    client = build_client(default_config(), user_agent=ua)
    try:
        actual = client.headers["user-agent"]
    finally:
        await client.aclose()
    assert actual == ua
    assert "Mozilla" not in actual


# ---------- 13. __all__ 与禁止项 ----------


def test_http_source_has_no_forbidden_constructs() -> None:
    """用例 13：http.py 中不得出现 asyncio.sleep 调用或 AsyncHTTPTransport(retries=...）。"""
    path = Path(__file__).resolve().parents[1] / "src" / "myscraper" / "http.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(ast.unparse(node.func))

    assert "asyncio.sleep" not in calls
    assert not any("AsyncHTTPTransport" in name for name in calls)
