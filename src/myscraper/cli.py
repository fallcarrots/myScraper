"""typer 命令行入口。

职责边界：
- 只做参数解析、配置装配、退出码转换；抓取逻辑全部在 runner，写出在 output。
- 退出码契约：0 全成功 / 1 部分失败 / 2 配置或输入错误 / 3 全部失败 /
  5 内部错误 / 130 SIGINT。`main` 以 `standalone_mode=False` 调用 app，
  捕获 `typer.Exit` 转换为进程退出码，避免被 typer 吞掉。
- 合规红线：不提供任何关闭 robots.txt 检查的开关。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from . import __version__
from .config import AppConfig, build_user_agent, load_config
from .errors import ConfigError, InputError
from .exitcodes import ExitCode, decide_exit_code
from .input import load_urls
from .logsetup import setup_logging
from .runner import RunOptions, crawl

__all__ = ["app", "main"]

app: typer.Typer = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="低并发、强合规的静态页面抓取工具（默认遵守 robots.txt）",
)


@app.command(name="crawl")
def crawl_cmd(
    urls_file: Annotated[
        Path | None, typer.Option("--urls", "-u", help="URL 列表文件；省略或 - 表示从 stdin 读取")
    ] = None,
    config_file: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    fmt: Annotated[str | None, typer.Option("--format", "-f")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    concurrency: Annotated[int | None, typer.Option("--concurrency", "-j")] = None,
    rate_limit: Annotated[float | None, typer.Option("--rate-limit")] = None,
    max_retries: Annotated[int | None, typer.Option("--max-retries")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    show_config: Annotated[bool, typer.Option("--show-config")] = False,
) -> None:
    """抓取 URL 列表并按指定格式写出结果。"""
    setup_logging(level="DEBUG" if verbose else "INFO")
    try:
        cfg = load_config(config_file)
        updates: dict[str, object] = {}
        if concurrency is not None:
            updates["max_concurrency"] = concurrency
        if rate_limit is not None:
            updates["rate_limit"] = {
                **cfg.rate_limit.model_dump(),
                "rate_limit_per_host": rate_limit,
            }
        if max_retries is not None:
            updates["retry"] = {**cfg.retry.model_dump(), "max_retries": max_retries}
        if fmt is not None:
            updates["output"] = {**cfg.output.model_dump(), "format": fmt}
        if updates:
            # 合并普通 dict 后重新校验：model_copy(update=...) 不校验，会绕过 Literal/le 约束。
            merged: dict[str, object] = cfg.model_dump()
            merged.update(updates)
            cfg = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise typer.Exit(_config_error_exit(f"配置校验失败: {exc}")) from exc
    except ConfigError as exc:
        raise typer.Exit(_config_error_exit(f"配置错误: {exc}")) from exc

    if show_config:
        typer.echo(json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False, indent=2))
        raise typer.Exit(0)

    try:
        urls = load_urls(urls_file)
    except InputError as exc:
        raise typer.Exit(_config_error_exit(f"输入错误: {exc}")) from exc

    ua = build_user_agent(__version__, "https://github.com/fallcarrots/myScraper")
    options = RunOptions(fmt=cfg.output.format, out=output)
    stats = asyncio.run(crawl(cfg, urls, options, user_agent=ua))
    if stats.write_errors:
        typer.echo(f"写出失败 {stats.write_errors} 条，输出不完整", err=True)
        raise typer.Exit(int(ExitCode.INTERNAL))
    code = decide_exit_code(total=stats.total, succeeded=stats.ok, aborted=stats.aborted)
    if code == int(ExitCode.CONFIG) and stats.skipped > 0:
        # 全部因 robots 合规跳过不是配置错误：合规跳过视为正常收尾。
        code = int(ExitCode.OK)
    if code != int(ExitCode.OK):
        typer.echo(
            f"汇总: 尝试 {stats.total}，成功 {stats.ok}，失败 {stats.failed}，"
            f"跳过 {stats.skipped}，中断 {stats.aborted}",
            err=True,
        )
    raise typer.Exit(code)


def _config_error_exit(message: str) -> int:
    """输出配置/输入类错误消息并返回退出码 2。"""
    typer.echo(message, err=True)
    return int(ExitCode.CONFIG)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：把 typer 的退出码转换为进程退出码。"""
    args: list[str] | None = None if argv is None else list(argv)
    if args is not None and not args:
        args = ["--help"]
    try:
        result = app(args=args, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except (ConfigError, InputError) as exc:
        typer.echo(f"错误: {exc}", err=True)
        return int(ExitCode.CONFIG)
    except KeyboardInterrupt:
        typer.echo("已中断", err=True)
        return 130
    except typer.Abort:
        typer.echo("已中断", err=True)
        return 1
    except Exception as exc:
        typer.echo(f"内部错误: {type(exc).__name__}: {exc}", err=True)
        return int(ExitCode.INTERNAL)
    if isinstance(result, int):
        return result
    return int(ExitCode.OK)
