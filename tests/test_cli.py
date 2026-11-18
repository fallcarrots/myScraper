"""cli 模块与包元数据测试：占位入口与 __version__ / __all__。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import myscraper
from myscraper import cli as cli_module
from myscraper.cli import main
from myscraper.config import AppConfig
from myscraper.runner import CrawlStats, RunOptions


def test_main_help_returns_zero() -> None:
    """main(["--help"]) 展示帮助并返回 0。"""
    assert main(["--help"]) == 0


def test_main_returns_zero_empty_argv() -> None:
    """main([]) 返回 0。"""
    assert main([]) == 0


def test_version_is_nonempty_str() -> None:
    """__version__ 是非空 str。"""
    assert isinstance(myscraper.__version__, str)
    assert myscraper.__version__


def test_all_is_version_only() -> None:
    """__all__ 仅导出 __version__。"""
    assert myscraper.__all__ == ["__version__"]


def test_config_error_returns_two(tmp_path: Path) -> None:
    """配置或输入错误返回退出码 2。"""
    assert main(["--config", str(tmp_path / "missing.yaml")]) == 2


def test_ignore_robots_option_is_not_supported() -> None:
    """合规红线：不提供关闭 robots 的开关。"""
    assert main(["--ignore-robots", "--show-config"]) != 0


# ---------------------------------------------------------------------------
# 追加用例：CLI 选项装配、配置优先级与退出码映射。
# ---------------------------------------------------------------------------


def _show_config(argv: Sequence[str], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """运行 `main([*argv, "--show-config"])` 并返回解析后的配置字典。"""
    code = main([*argv, "--show-config"])
    assert code == 0
    captured = capsys.readouterr()
    payload: object = json.loads(captured.out)
    assert isinstance(payload, dict)
    return {str(key): value for key, value in payload.items()}


def _write_url_file(tmp_path: Path) -> Path:
    """写一个只含单条 https URL 的输入文件。"""
    path = tmp_path / "urls.txt"
    path.write_text("https://a.example/x\n", encoding="utf-8")
    return path


def _nested(config: dict[str, Any], key: str) -> dict[str, Any]:
    """取出配置里的嵌套字典，缺失或类型不符时直接失败。"""
    value = config[key]
    assert isinstance(value, dict)
    return {str(name): item for name, item in value.items()}


def _install_fake_crawl(
    monkeypatch: pytest.MonkeyPatch,
    outcome: CrawlStats | Exception,
) -> None:
    """把 `myscraper.cli.crawl` 换成不触网的假实现：返回 outcome 或抛出它。"""

    async def fake_crawl(
        cfg: AppConfig,
        urls: Sequence[str],
        options: RunOptions,
        **kwargs: object,
    ) -> CrawlStats:
        del cfg, urls, options, kwargs
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cli_module, "crawl", fake_crawl)


def test_show_config_concurrency_override(capsys: pytest.CaptureFixture[str]) -> None:
    """--concurrency 4 写入 max_concurrency。"""
    config = _show_config(["--concurrency", "4"], capsys)
    assert config["max_concurrency"] == 4


def test_show_config_rate_limit_override(capsys: pytest.CaptureFixture[str]) -> None:
    """--rate-limit 2.5 写入 rate_limit.rate_limit_per_host。"""
    config = _show_config(["--rate-limit", "2.5"], capsys)
    assert _nested(config, "rate_limit")["rate_limit_per_host"] == 2.5


def test_show_config_max_retries_override(capsys: pytest.CaptureFixture[str]) -> None:
    """--max-retries 5 写入 retry.max_retries。"""
    config = _show_config(["--max-retries", "5"], capsys)
    assert _nested(config, "retry")["max_retries"] == 5


def test_show_config_format_csv(capsys: pytest.CaptureFixture[str]) -> None:
    """--format csv 写入 output.format。"""
    config = _show_config(["--format", "csv"], capsys)
    assert _nested(config, "output")["format"] == "csv"


def test_invalid_format_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """--format xml 不是合法 Literal，期望退出码 2。"""
    assert main(["--format", "xml", "--show-config"]) == 2


def test_concurrency_above_hard_cap_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """--concurrency 65 超过硬上限 64，期望退出码 2。"""
    assert main(["--concurrency", "65", "--show-config"]) == 2


def test_rate_limit_zero_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """--rate-limit 0 违反 gt=0 约束，期望退出码 2。"""
    assert main(["--rate-limit", "0", "--show-config"]) == 2


def test_config_file_sets_concurrency(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """YAML 配置文件的 max_concurrency 生效。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("max_concurrency: 4\n", encoding="utf-8")
    config = _show_config(["--config", str(config_file)], capsys)
    assert config["max_concurrency"] == 4


def test_cli_overrides_config_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """命令行 --concurrency 覆盖 YAML 配置。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("max_concurrency: 4\n", encoding="utf-8")
    config = _show_config(["--config", str(config_file), "--concurrency", "12"], capsys)
    assert config["max_concurrency"] == 12


def test_verbose_show_config_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    """--verbose 与 --show-config 可共存，且输出合法 JSON。"""
    config = _show_config(["--verbose"], capsys)
    assert config["max_concurrency"] >= 1


def test_partial_failure_exit_code_is_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有成功也有失败时返回 1（部分失败）。"""
    _install_fake_crawl(
        monkeypatch,
        CrawlStats(total=2, ok=1, failed=1, skipped=0, aborted=False),
    )
    assert main(["--urls", str(_write_url_file(tmp_path))]) == 1


def test_all_failed_exit_code_is_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部失败时返回 3。"""
    _install_fake_crawl(
        monkeypatch,
        CrawlStats(total=2, ok=0, failed=2, skipped=0, aborted=False),
    )
    assert main(["--urls", str(_write_url_file(tmp_path))]) == 3


def test_crawl_exception_exit_code_is_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """crawl 抛未预期异常时返回 5（内部错误）。"""
    _install_fake_crawl(monkeypatch, RuntimeError("boom"))
    assert main(["--urls", str(_write_url_file(tmp_path))]) == 5


def test_write_errors_exit_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """写出失败时返回退出码 5，并在 stderr 提示写出失败。"""
    _install_fake_crawl(
        monkeypatch,
        CrawlStats(total=1, ok=1, failed=0, skipped=0, write_errors=2),
    )
    assert main(["--urls", str(_write_url_file(tmp_path))]) == 5
    assert "写出失败" in capsys.readouterr().err


def test_config_file_format_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """YAML 配置文件的 output.format 生效并传给 crawl。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("output:\n  format: csv\n", encoding="utf-8")
    seen: list[RunOptions] = []

    async def fake_crawl(
        cfg: AppConfig,
        urls: Sequence[str],
        options: RunOptions,
        **kwargs: object,
    ) -> CrawlStats:
        del cfg, urls, kwargs
        seen.append(options)
        return CrawlStats(total=1, ok=1, failed=0, skipped=0)

    monkeypatch.setattr(cli_module, "crawl", fake_crawl)
    assert main(["--config", str(config_file), "--urls", str(_write_url_file(tmp_path))]) == 0
    assert seen[0].fmt == "csv"


def test_all_skipped_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部因 robots 合规跳过时返回退出码 0。"""
    _install_fake_crawl(
        monkeypatch,
        CrawlStats(total=0, ok=0, failed=0, skipped=3),
    )
    assert main(["--urls", str(_write_url_file(tmp_path))]) == 0
