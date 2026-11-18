"""抓取主编排器（runner）：依赖装配 + 固定 worker 池 + 单消费者写队列。

并发与关闭模型（架构裁定）：
1. **固定 worker 池即并发上限**：启动 ``cfg.max_concurrency`` 个 worker 任务，
   **禁止叠加 ``asyncio.Semaphore``**——worker 数量本身就是并发上限，
   信号量只会引入冗余排队点。
2. **单消费者写队列**：worker 把 `PageRecord` 投入有界 ``asyncio.Queue``，由
   唯一 writer 协程串行调用 ``writer.write``。选队列而非 ``asyncio.Lock`` 的
   理由：有界队列对写入侧提供天然背压（worker 不会无限囤积未落盘记录，
   内存 O(1)），且同步 I/O 收敛到单一协程，无需在每个 worker 的关键区里
   持锁执行阻塞写。
3. **哨兵关闭**：URL 投递完后按 worker 数各投一个 ``None`` 哨兵；worker 全部
   退出后再向结果队列投一个 ``None`` 结束 writer 协程，保证全部 ``write``
   完成后才收尾。
4. **close 责任**：注入的 client / writer 由调用方负责关闭；自建 client 在
   ``finally`` 中 ``aclose``；自建 writer 经 ``open_writer`` 上下文管理器在
   所有路径（含取消）统一 close——语义等价于"仅当由本函数创建时才 close"，
   且避免 `JsonArrayWriter.close` 被双重调用损坏输出。
5. 所有等待一律经注入的 `Clock`（limiter / retry 内部完成），本模块不出现裸
   ``asyncio.sleep``；文件写出全部由 writer 负责，本模块不做任何落盘 I/O。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
from loguru import logger

from .clock import Clock, SystemClock
from .config import AppConfig
from .errors import MissingRequired
from .extract import MultiPlan, ScalarPlan, compile_plan, extract_page
from .http import FetchOutcome, HttpFetcher, build_client
from .models import PageRecord
from .output import RecordWriter, open_writer
from .progress import NullProgress, ProgressReporter
from .ratelimit import HostRateLimiter
from .retry import RetryPolicy
from .robots import RobotsCache

__all__ = ["CrawlStats", "RunOptions", "crawl"]


@dataclass(frozen=True, slots=True)
class RunOptions:
    """单次运行的 CLI 级选项（合规裁定：不提供 ignore_robots 之类开关）。"""

    #: 输出格式：jsonl / json / csv（交由 `open_writer` 校验）。
    fmt: str = "jsonl"
    #: 输出路径；None 或 "-" 时写 stdout。
    out: Path | None = None


@dataclass(frozen=True, slots=True)
class CrawlStats:
    """单次运行的统计汇总。"""

    #: 已尝试条数（ok + failed），不含 skipped。
    total: int
    #: 成功抓取并抽取的条数。
    ok: int
    #: 请求失败或抽取失败的条数。
    failed: int
    #: 因合规/限制被跳过的条数。
    skipped: int
    #: 运行是否被取消中断（取消路径下 crawl 会重抛 CancelledError）。
    aborted: bool = False


def _to_record(
    outcome: FetchOutcome,
    clk: Clock,
    plan: dict[str, ScalarPlan | MultiPlan] | None,
    cfg: AppConfig,
) -> PageRecord:
    """把 `FetchOutcome` 转成 `PageRecord`；ok 记录现场执行抽取。

    抽取异常处理：`MissingRequired` → ``failed``，error 为异常消息；
    其他 `Exception` → ``failed``，error 为 ``extract/<异常类型名>``。
    """
    fetched_at = clk.now()
    if outcome.status == "skipped":
        return PageRecord(
            url=outcome.url,
            final_url=outcome.final_url,
            status_code=outcome.status_code,
            fetched_at=fetched_at,
            status="skipped",
            skip_reason=outcome.skip_reason,
        )
    if outcome.status == "failed":
        return PageRecord(
            url=outcome.url,
            final_url=outcome.final_url,
            status_code=outcome.status_code,
            fetched_at=fetched_at,
            status="failed",
            error=outcome.error,
        )
    try:
        res = extract_page(
            outcome.text,
            outcome.final_url,
            plan=plan,
            max_text_chars=cfg.extract.max_text_chars,
            max_links=cfg.extract.max_links,
        )
    except MissingRequired as exc:
        return PageRecord(
            url=outcome.url,
            final_url=outcome.final_url,
            status_code=outcome.status_code,
            fetched_at=fetched_at,
            status="failed",
            error=str(exc),
        )
    except Exception as exc:
        return PageRecord(
            url=outcome.url,
            final_url=outcome.final_url,
            status_code=outcome.status_code,
            fetched_at=fetched_at,
            status="failed",
            error=f"extract/{type(exc).__name__}",
        )
    return PageRecord(
        url=outcome.url,
        final_url=outcome.final_url,
        status_code=outcome.status_code,
        title=res.title,
        text=res.text,
        links=list(res.links),
        fetched_at=fetched_at,
        status="ok",
    )


async def _run_crawl(
    cfg: AppConfig,
    urls: Sequence[str],
    *,
    clock: Clock | None,
    client: httpx.AsyncClient | None,
    writer: RecordWriter,
    progress: ProgressReporter | None,
    user_agent: str,
) -> CrawlStats:
    """装配依赖并运行 worker 池 + 单消费者写协程（writer 已就绪）。"""
    clk: Clock = clock if clock is not None else SystemClock()
    own_client = client is None
    http = client if client is not None else build_client(cfg, user_agent=user_agent)
    # 全局速率兜底与 per-host 限速在同一把锁内以 max() 复合，worker 数与 host 数解耦。
    limiter = HostRateLimiter(
        clk,
        rate_limit_per_host=cfg.rate_limit.rate_limit_per_host,
        min_request_interval=cfg.rate_limit.min_request_interval,
        global_rate_limit=cfg.rate_limit.global_rate_limit,
    )
    policy = RetryPolicy(
        max_retries=cfg.retry.max_retries,
        base_backoff_s=cfg.retry.base_backoff_s,
        max_backoff_s=cfg.retry.max_backoff_s,
    )
    robots = RobotsCache(
        http,
        clk,
        ttl_s=cfg.robots.cache_ttl_s,
        max_crawl_delay_s=cfg.robots.max_crawl_delay_s,
    )
    fetcher = HttpFetcher(
        http,
        limiter,
        policy,
        robots,
        clk,
        rate_limit_per_host=cfg.rate_limit.rate_limit_per_host,
        min_request_interval=cfg.rate_limit.min_request_interval,
        max_html_bytes=cfg.http.max_html_bytes,
    )
    plan: dict[str, ScalarPlan | MultiPlan] | None = (
        compile_plan(cfg.extract.selectors, max_links=cfg.extract.max_links)
        if cfg.extract.selectors
        else None
    )
    prog: ProgressReporter = progress if progress is not None else NullProgress()
    prog.start(len(urls))

    work: asyncio.Queue[str | None] = asyncio.Queue(maxsize=cfg.max_concurrency * 2)
    results: asyncio.Queue[PageRecord | None] = asyncio.Queue(maxsize=cfg.max_concurrency * 2)

    async def worker() -> tuple[int, int, int]:
        """逐个消费 URL：fetch → 转记录 → 入写队列 → 推进进度；返回本 worker 计数。"""
        ok = 0
        failed = 0
        skipped = 0
        while True:
            url = await work.get()
            if url is None:
                return ok, failed, skipped
            outcome = await fetcher.fetch(url)
            record = _to_record(outcome, clk, plan, cfg)
            await results.put(record)
            prog.advance(record)
            if record.status == "ok":
                ok += 1
            elif record.status == "failed":
                failed += 1
            else:
                skipped += 1

    async def writer_loop() -> None:
        """唯一消费者：串行调用 writer.write，直到收到 None 哨兵。

        单条写失败仅记日志并丢弃该条，不终止消费循环（避免 worker 因
        ``results.put`` 永久阻塞造成死锁）。
        """
        while True:
            rec = await results.get()
            if rec is None:
                return
            try:
                writer.write(rec)
            except Exception as exc:
                logger.error("写出记录失败，该条已丢弃: {}", exc)

    workers: list[asyncio.Task[tuple[int, int, int]]] = [
        asyncio.create_task(worker(), name=f"myscraper-worker-{i}")
        for i in range(cfg.max_concurrency)
    ]
    writer_task: asyncio.Task[None] = asyncio.create_task(writer_loop(), name="myscraper-writer")
    aborted = False
    ok_total = 0
    failed_total = 0
    skipped_total = 0
    try:
        for url in urls:
            await work.put(url)
        for _ in workers:
            await work.put(None)
        counts = await asyncio.gather(*workers)
        ok_total = sum(ok for ok, _failed, _skipped in counts)
        failed_total = sum(failed for _ok, failed, _skipped in counts)
        skipped_total = sum(skipped for _ok, _failed, skipped in counts)
        await results.put(None)
        await writer_task
    except asyncio.CancelledError:
        aborted = True
        logger.warning("抓取被取消/中断：未完成 URL 丢弃，已入队未写出的记录将丢失")
        raise
    finally:
        for t in workers:
            t.cancel()
        writer_task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*workers, writer_task, return_exceptions=True)
        if own_client:
            with suppress(asyncio.CancelledError):
                await http.aclose()
        prog.close()
    return CrawlStats(
        total=ok_total + failed_total,
        ok=ok_total,
        failed=failed_total,
        skipped=skipped_total,
        aborted=aborted,
    )


async def crawl(
    cfg: AppConfig,
    urls: Sequence[str],
    options: RunOptions,
    *,
    clock: Clock | None = None,
    client: httpx.AsyncClient | None = None,
    writer: RecordWriter | None = None,
    progress: ProgressReporter | None = None,
    user_agent: str = "myscraper",
) -> CrawlStats:
    """编排一次完整抓取：依赖装配 → worker 池消费 URL → 串行写出 → 汇总统计。

    写串行化采用**单消费者队列**（而非 ``asyncio.Lock``）：有界结果队列对
    worker 提供背压，内存 O(1)；同步写收敛到单一 writer 协程。
    自建 writer 经 ``open_writer`` 上下文在所有路径统一 close（含取消），
    注入的 writer 由调用方负责 close。

    Args:
        cfg: 应用配置（并发数、限速、重试、robots、抽取限制）。
        urls: 待抓取 URL 序列。
        options: CLI 级输出选项（fmt / out）。
        clock: 可注入时间源；None 时自建 `SystemClock`。
        client: 可注入 httpx 异步客户端；None 时自建并在结束时 aclose。
        writer: 可注入记录写入器；None 时按 options 自建。
        progress: 可注入进度报告器；None 时用 `NullProgress`。
        user_agent: 自建 client 的 User-Agent。

    Returns:
        `CrawlStats`：``total = ok + failed``（不含 skipped）。

    Raises:
        asyncio.CancelledError: 任务被取消（如 SIGINT）时置 aborted 后原样
            重抛；``KeyboardInterrupt`` 由 CLI 层转换为退出码 130。
        ConfigError: ``options.fmt`` 非 jsonl/json/csv 时（由 open_writer 抛出）。
    """
    if writer is not None:
        return await _run_crawl(
            cfg,
            urls,
            clock=clock,
            client=client,
            writer=writer,
            progress=progress,
            user_agent=user_agent,
        )
    with open_writer(options.fmt, options.out, csv_utf8_bom=cfg.output.csv_utf8_bom) as own_writer:
        return await _run_crawl(
            cfg,
            urls,
            clock=clock,
            client=client,
            writer=own_writer,
            progress=progress,
            user_agent=user_agent,
        )
