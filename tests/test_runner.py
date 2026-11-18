"""runner 模块测试：crawl 编排全链路（MockTransport + FakeClock，零真实网络、零真实 sleep）。

网络一律走 httpx.MockTransport；时间一律注入 FakeClock（sleep 只推进虚拟时间）；
写出用 io.StringIO + JsonlWriter 或内存 ListWriter；进度用 NullProgress。
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from fakes import FakeClock

from myscraper.config import (
    AppConfig,
    ExtractSettings,
    RateLimitSettings,
    RetrySettings,
    default_config,
)
from myscraper.models import PageRecord
from myscraper.output import JsonlWriter
from myscraper.progress import NullProgress
from myscraper.runner import RunOptions, crawl

#: 供源码红线断言使用的 runner.py 路径。
RUNNER_SOURCE: Final[Path] = Path(__file__).resolve().parents[1] / "src/myscraper/runner.py"

#: 含 <title> 的正常页面。
PAGE_HTML: Final[str] = "<html><head><title>T</title></head><body>hello</body></html>"
#: 无 <title> 也无 <h1> 的页面（配合 required 选择器触发抽取失败）。
NO_TITLE_HTML: Final[str] = "<html><body><p>plain text only</p></body></html>"
#: robots.txt：全站放行。
ROBOTS_ALLOW_ALL: Final[str] = "User-agent: *\nDisallow:\n"
#: robots.txt：全站禁止。
ROBOTS_DISALLOW_ALL: Final[str] = "User-agent: *\nDisallow: /\n"


class _ListWriter:
    """测试用内存 writer：收集 PageRecord，结构化满足 RecordWriter 协议。"""

    def __init__(self) -> None:
        self.records: list[PageRecord] = []

    def write(self, record: PageRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        return None


class _InFlight:
    """在途请求计数器：async transport 内统计并发峰值。"""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0


class _CountingTransport(httpx.AsyncBaseTransport):
    """异步 transport：记录在途请求峰值，用于验证 worker 池并发上限。"""

    def __init__(self, counter: _InFlight) -> None:
        super().__init__()
        self._counter = counter

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        self._counter.active += 1
        self._counter.peak = max(self._counter.peak, self._counter.active)
        try:
            await asyncio.sleep(0)  # 让出事件循环，制造真实并发窗口
            await asyncio.sleep(0)
        finally:
            self._counter.active -= 1
        return httpx.Response(200, text=PAGE_HTML)


def _make_cfg(
    *,
    max_concurrency: int = 4,
    selectors: dict[str, object] | None = None,
) -> AppConfig:
    """构造测试配置：高限速（间隔归零）、零重试，可覆盖并发数与选择器。"""
    update: dict[str, object] = {
        "max_concurrency": max_concurrency,
        "rate_limit": RateLimitSettings(rate_limit_per_host=1000.0, min_request_interval=0.0),
        "retry": RetrySettings(max_retries=0),
    }
    if selectors is not None:
        update["extract"] = ExtractSettings(selectors=selectors)
    return default_config().model_copy(update=update)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """用 httpx.MockTransport 构造不触网的 AsyncClient（与 build_client 行为对齐）。"""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True, max_redirects=5
    )


def _wrap_robots(
    page_handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """包装 handler：/robots.txt 统一放行，其余路径交给给定逻辑。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return page_handler(request)

    return handler


# ---------- 1. 全部成功 ----------


async def test_all_ok_two_urls() -> None:
    """用例 1：2 个 URL 均返回 200 + 含 <title> 的 HTML → ok=2/failed=0/skipped=0/total=2。"""
    cfg = _make_cfg()
    clock = FakeClock()
    writer = _ListWriter()
    client = _make_client(_wrap_robots(lambda _req: httpx.Response(200, text=PAGE_HTML)))
    try:
        stats = await crawl(
            cfg,
            ["https://a.example/p1", "https://a.example/p2"],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
    assert stats.ok == 2
    assert stats.failed == 0
    assert stats.skipped == 0
    assert stats.total == 2
    assert stats.aborted is False
    assert [r.title for r in writer.records] == ["T", "T"]


# ---------- 2. 部分失败 ----------


async def test_partial_failure_500_no_retry() -> None:
    """用例 2：一 200 一 500（max_retries=0）→ ok=1、failed=1、total=2。"""
    cfg = _make_cfg()

    def page(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bad":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=PAGE_HTML)

    clock = FakeClock()
    writer = _ListWriter()
    client = _make_client(_wrap_robots(page))
    try:
        stats = await crawl(
            cfg,
            ["https://a.example/ok", "https://a.example/bad"],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
    assert stats.ok == 1
    assert stats.failed == 1
    assert stats.total == 2
    assert stats.aborted is False
    statuses = sorted(r.status for r in writer.records)
    assert statuses == ["failed", "ok"]


# ---------- 3. robots 禁止 → skipped ----------


async def test_robots_disallow_all_skipped() -> None:
    """用例 3：robots.txt 全站禁止 → skipped=1、total=0，且不发起任何页面请求。"""
    cfg = _make_cfg()
    page_calls: list[str] = []

    def page(request: httpx.Request) -> httpx.Response:
        page_calls.append(request.url.path)
        return httpx.Response(200, text=PAGE_HTML)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_DISALLOW_ALL)
        return page(request)

    clock = FakeClock()
    writer = _ListWriter()
    client = _make_client(handler)
    try:
        stats = await crawl(
            cfg,
            ["https://a.example/p1"],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
    assert stats.skipped == 1
    assert stats.total == 0
    assert stats.ok == 0
    assert stats.failed == 0
    assert stats.aborted is False
    assert page_calls == []  # 被禁页面零请求
    assert [r.status for r in writer.records] == ["skipped"]


# ---------- 4. 抽取失败 → failed 记录 ----------


async def test_extract_missing_required_failed() -> None:
    """用例 4：无 <title> 且 title 槽位 required → 记录 failed，error 前缀合规。"""
    cfg = _make_cfg(selectors={"title": {"selector": "h1", "required": True}})
    clock = FakeClock()
    writer = _ListWriter()
    client = _make_client(_wrap_robots(lambda _req: httpx.Response(200, text=NO_TITLE_HTML)))
    try:
        stats = await crawl(
            cfg,
            ["https://a.example/notitle"],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
    assert stats.failed == 1
    assert stats.ok == 0
    failed = [r for r in writer.records if r.status == "failed"]
    assert len(failed) == 1
    error = failed[0].error
    assert error is not None
    assert error.startswith("extract/") or error.startswith("必填字段未命中")


# ---------- 5. 空 URL 列表 ----------


async def test_empty_url_list_noop() -> None:
    """用例 5：urls=[] → 全部计数为 0，不抛异常。"""
    cfg = _make_cfg()
    clock = FakeClock()
    writer = _ListWriter()
    client = _make_client(_wrap_robots(lambda _req: httpx.Response(200, text=PAGE_HTML)))
    try:
        stats = await crawl(
            cfg,
            [],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
    assert stats.total == 0
    assert stats.ok == 0
    assert stats.failed == 0
    assert stats.skipped == 0
    assert stats.aborted is False
    assert writer.records == []


# ---------- 6. 写入条数（JSONL） ----------


async def test_jsonl_writer_exactly_two_lines() -> None:
    """用例 6：2 个成功 URL + JsonlWriter → 输出恰好 2 行，每行可 json.loads。"""
    cfg = _make_cfg()
    clock = FakeClock()
    buf = io.StringIO()
    writer = JsonlWriter(buf)
    client = _make_client(_wrap_robots(lambda _req: httpx.Response(200, text=PAGE_HTML)))
    try:
        stats = await crawl(
            cfg,
            ["https://a.example/p1", "https://a.example/p2"],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
        writer.close()
    assert stats.ok == 2
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert rec["status"] == "ok"
        assert rec["title"] == "T"


# ---------- 7. 并发上限生效 ----------


async def test_concurrency_cap_respected() -> None:
    """用例 7：max_concurrency=2、5 个 URL → 在途请求峰值不超过 2，且池确实并发到 2。"""
    cfg = _make_cfg(max_concurrency=2)
    counter = _InFlight()

    clock = FakeClock()
    writer = _ListWriter()
    client = httpx.AsyncClient(transport=_CountingTransport(counter))
    try:
        stats = await crawl(
            cfg,
            [f"https://a.example/p{i}" for i in range(5)],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
    finally:
        await client.aclose()
    assert stats.ok == 5
    assert stats.failed == 0
    assert counter.peak <= 2, f"在途请求峰值 {counter.peak} 超过并发上限 2"
    assert counter.peak >= 2, "固定 worker 池应允许 2 个请求同时在途（峰值未达上限）"


# ---------- 8. 注入的 client 不被关闭 ----------


async def test_injected_client_still_usable_after_crawl() -> None:
    """用例 8：注入自建 client，crawl 返回后仍可发请求（crawl 不代为关闭）。"""
    cfg = _make_cfg()
    clock = FakeClock()
    writer = _ListWriter()
    client = _make_client(_wrap_robots(lambda _req: httpx.Response(200, text=PAGE_HTML)))
    try:
        stats = await crawl(
            cfg,
            ["https://a.example/p1"],
            RunOptions(),
            clock=clock,
            client=client,
            writer=writer,
            progress=NullProgress(),
        )
        assert stats.ok == 1
        # crawl 返回后 client 仍可用：不抛 "client has been closed"。
        resp = await client.get("https://a.example/after-crawl")
        assert resp.status_code == 200
    finally:
        await client.aclose()


# ---------- 9. 源码红线 ----------


def test_runner_source_forbids_semaphore() -> None:
    """用例 9：源码红线——runner.py 不得出现 Semaphore(（worker 池即并发上限）。"""
    text = RUNNER_SOURCE.read_text(encoding="utf-8")
    assert "Semaphore(" not in text
