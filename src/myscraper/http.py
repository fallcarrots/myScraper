"""HTTP 客户端构造与限长读取。

合规红线：**禁止使用 ``httpx.AsyncHTTPTransport(retries=...)``**——
该重试不识别 ``Retry-After`` 响应头，且会在传输层绕过仓库自有的限速器
（``ratelimit``），破坏每主机速率限制与退避语义。重试一律由上层通过
注入的 ``Clock`` 显式控制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from .clock import Clock
from .config import AppConfig
from .ratelimit import HostRateLimiter
from .retry import RetryPolicy, parse_retry_after, should_retry
from .robots import RobotsCache

__all__ = [
    "FetchOutcome",
    "FetchStatus",
    "HttpFetcher",
    "build_client",
    "read_capped",
]

#: 抓取结果的三态：成功 / 因合规或限制被跳过 / 请求失败。
FetchStatus = Literal["ok", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """单次 URL 抓取的最终结果。"""

    #: 原始请求 URL。
    url: str
    #: 重定向后实际到达的 URL。
    final_url: str
    #: 响应状态码（失败且未到达响应时为 0）。
    status_code: int
    #: 解码后的页面文本（默认为空）。
    text: str = ""
    #: 抓取三态结果。
    status: FetchStatus = "ok"
    #: 跳过原因（status == "skipped" 时提供）。
    skip_reason: str | None = None
    #: 错误描述（status == "failed" 时提供）。
    error: str | None = None
    #: 响应体是否超过限额被截断。
    truncated: bool = False
    #: 实际尝试次数（默认 1 次）。
    attempts: int = 1


def build_client(cfg: AppConfig, *, user_agent: str) -> httpx.AsyncClient:
    """按配置构造异步 HTTP 客户端（仅设置 User-Agent，无其他自定义头）。"""
    timeout = httpx.Timeout(
        connect=cfg.http.connect_timeout_s,
        read=cfg.http.read_timeout_s,
        write=cfg.http.read_timeout_s,
        pool=cfg.http.connect_timeout_s,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": user_agent},
    )


async def read_capped(resp: httpx.Response, limit: int) -> tuple[str, bool]:
    """流式读取响应体，超过 ``limit`` 字节即截断。

    返回 ``(解码文本, 是否截断)``；解码按响应字符集，未知时回退 UTF-8，
    非法字节以替换字符容错。
    """
    encoding = resp.charset_encoding or "utf-8"
    chunks: list[bytes] = []
    received = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        received += len(chunk)
        if received > limit:
            overflow = received - limit
            keep = chunk[:-overflow] if overflow < len(chunk) else b""
            if keep:
                chunks.append(keep)
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(encoding, errors="replace"), truncated


class HttpFetcher:
    """单 URL 抓取编排器：robots 合规 → 每次尝试限速 → 重试决策 → 限长读取。

    流程严格按序执行：
    1. robots 判定，不允许即返回 ``skipped``；
    2. 逐次尝试（最多 ``max_retries + 1`` 次），**每次尝试都重新 acquire 令牌**，
       并向限速器透传 robots 的 ``crawl_delay``（秒），间隔公式由限速器内部统一计算；
    3. 网络异常与可重试状态码按 `RetryPolicy` 退避（经注入 `Clock` 睡眠，
       绝不直接 ``asyncio.sleep``，也不使用 ``AsyncHTTPTransport(retries=)``）；
    4. 2xx 成功时限长读取响应体，并复核重定向终点的 robots 合规；
    5. 循环耗尽后返回最后一次失败的 `FetchOutcome`。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: HostRateLimiter,
        policy: RetryPolicy,
        robots: RobotsCache,
        clock: Clock,
        *,
        rate_limit_per_host: float,
        min_request_interval: float,
        max_html_bytes: int,
    ) -> None:
        """初始化抓取器。

        Args:
            client: 已构造好的 httpx 异步客户端（重定向由其跟随）。
            limiter: 每主机限速器，每次请求尝试前都必须 acquire。
            policy: 重试策略，决定是否重试及退避时长。
            robots: robots.txt 裁决缓存，请求前与重定向终点复核各用一次。
            clock: 注入的时间源，仅经其完成等待与 Retry-After 解析取当前时间。
            rate_limit_per_host: 每主机每秒最大请求数（正数）。
            min_request_interval: 两次请求之间的硬性最小间隔（秒）。
            max_html_bytes: 响应体最大读取字节数，超出即截断。
        """
        self._client = client
        self._limiter = limiter
        self._policy = policy
        self._robots = robots
        self._clock = clock
        self._rate_limit_per_host = rate_limit_per_host
        self._min_request_interval = min_request_interval
        self._max_html_bytes = max_html_bytes

    async def fetch(self, url: str) -> FetchOutcome:
        """抓取单个 URL，返回 `FetchOutcome`（绝不抛出网络/robots 异常，含 InvalidURL）。

        Args:
            url: 原始请求 URL。

        Returns:
            `FetchOutcome`：``ok`` / ``skipped`` / ``failed`` 三态之一；
            所有重试耗尽后返回最后一次失败的快照。
        """
        host = urlsplit(url).netloc

        # 1) robots 合规判定（不允许即跳过，不发起任何网络抓取请求）。
        verdict = await self._robots.verdict_for(url)
        if not verdict.allowed:
            return FetchOutcome(
                status="skipped",
                skip_reason=f"robots/{verdict.source}",
                status_code=0,
                url=url,
                final_url=url,
            )

        last_outcome: FetchOutcome | None = None

        # 2) 逐次尝试：首次 + 最多 max_retries 次重试；每次尝试都重新取令牌。
        #    host 步长由限速器内部统一计算，这里只透传 robots 的 crawl_delay（秒）。
        for attempt in range(1, self._policy.max_retries + 2):
            await self._limiter.acquire(host, crawl_delay=verdict.crawl_delay_s)

            try:
                resp = await self._client.get(url)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                # 2a) 网络层失败（含构造 Request 时同步抛出的 InvalidURL，
                #     它不继承 HTTPError）：可重试则按策略退避后继续，否则终局失败。
                if should_retry(
                    "GET",
                    status_code=None,
                    attempt=attempt,
                    exc=exc,
                    max_retries=self._policy.max_retries,
                ):
                    last_outcome = FetchOutcome(
                        status="failed",
                        error=f"network/{type(exc).__name__}",
                        status_code=0,
                        url=url,
                        final_url=url,
                        attempts=attempt,
                    )
                    await self._clock.sleep(self._policy.delay_for(attempt, retry_after_s=None))
                    continue
                return FetchOutcome(
                    status="failed",
                    error=f"network/{type(exc).__name__}",
                    status_code=0,
                    url=url,
                    final_url=url,
                    attempts=attempt,
                )

            if 200 <= resp.status_code < 300:
                # 2b) 成功：限长读取响应体。
                text, truncated = await read_capped(resp, self._max_html_bytes)
                # 重定向终点再复核一次 robots 合规。
                final_url = str(resp.url)
                if final_url != url:
                    redirect_verdict = await self._robots.verdict_for(final_url)
                    if not redirect_verdict.allowed:
                        return FetchOutcome(
                            status="skipped",
                            skip_reason="robots/redirect_disallowed",
                            status_code=resp.status_code,
                            url=url,
                            final_url=final_url,
                            text=text,
                            truncated=truncated,
                            attempts=attempt,
                        )
                return FetchOutcome(
                    status="ok",
                    status_code=resp.status_code,
                    url=url,
                    final_url=final_url,
                    text=text,
                    truncated=truncated,
                    attempts=attempt,
                )

            # 2c) 可重试状态码：按 Retry-After（若有）或指数退避等待后继续。
            if should_retry(
                "GET",
                status_code=resp.status_code,
                attempt=attempt,
                exc=None,
                max_retries=self._policy.max_retries,
            ):
                retry_after = parse_retry_after(
                    resp.headers.get("Retry-After"), now=self._clock.now()
                )
                last_outcome = FetchOutcome(
                    status="failed",
                    error=f"http/{resp.status_code}",
                    status_code=resp.status_code,
                    url=url,
                    final_url=str(resp.url),
                    attempts=attempt,
                )
                await self._clock.sleep(self._policy.delay_for(attempt, retry_after_s=retry_after))
                continue

            # 2d) 不可重试的状态码：终局失败。
            return FetchOutcome(
                status="failed",
                error=f"http/{resp.status_code}",
                status_code=resp.status_code,
                url=url,
                final_url=str(resp.url),
                attempts=attempt,
            )

        # 3) 循环耗尽：兜底返回最后一次失败快照。
        if last_outcome is None:
            return FetchOutcome(
                status="failed",
                error="retry/exhausted",
                status_code=0,
                url=url,
                final_url=url,
                attempts=self._policy.max_retries + 1,
            )
        return last_outcome
