"""logsetup 模块测试：仅使用 loguru 公开 API，通过 capsys 捕获 stderr 断言。

验证点：
- 默认 stderr sink 已被 setup_logging 内的 logger.remove() 清掉（不重复输出）；
- setup_logging 幂等（连续调用不会叠加 sink 导致双份输出）；
- json_mode 输出为可解析 JSON 行；
- level 参数生效；
- 绝不添加文件 sink（不落盘日志）；
- get_logger 绑定 component 字段。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from myscraper.logsetup import get_logger, setup_logging


@pytest.fixture(autouse=True)
def _reset_logger() -> Iterator[None]:
    """每条用例前后 remove 全部 handler，避免默认 sink / 残留 sink 污染断言。"""
    logger.remove()
    yield
    logger.remove()


def test_setup_logging_no_duplicate_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """核心回归：默认 sink 已被 remove，日志只输出一份。"""
    setup_logging()
    logger.info("uniq-token-abc")
    err = capsys.readouterr().err
    assert err.count("uniq-token-abc") == 1


def test_setup_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """连续调用 setup_logging() 两次，日志仍只输出一份。"""
    setup_logging()
    setup_logging()
    logger.info("idem-token")
    err = capsys.readouterr().err
    assert err.count("idem-token") == 1


def test_json_mode_output_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    """json_mode=True 时输出为 JSON 行，含消息字段。"""
    setup_logging(json_mode=True)
    logger.info("uniq-token-xyz")
    err = capsys.readouterr().err
    last_line = err.strip().splitlines()[-1]
    record = json.loads(last_line)
    text = json.dumps(record, ensure_ascii=False)
    assert "uniq-token-xyz" in text


def test_level_filters_messages(capsys: pytest.CaptureFixture[str]) -> None:
    """level="DEBUG" 时 debug 可见；level="WARNING" 时 debug 被过滤。"""
    setup_logging(level="DEBUG")
    logger.debug("dbg-token")
    err = capsys.readouterr().err
    assert "dbg-token" in err

    setup_logging(level="WARNING")
    logger.debug("hidden-token")
    err = capsys.readouterr().err
    assert "hidden-token" not in err


def test_no_file_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """合规约束：不添加任何文件 sink（不落盘日志）。"""
    monkeypatch.chdir(tmp_path)
    setup_logging()
    logger.info("file-sink-check")
    assert list(tmp_path.iterdir()) == []


def test_get_logger_binds_component(capsys: pytest.CaptureFixture[str]) -> None:
    """get_logger 绑定 component，输出含组件名与消息。"""
    setup_logging()
    get_logger("config").info("bind-token")
    err = capsys.readouterr().err
    assert "bind-token" in err

    # 通过公开 API 临时挂 sink 验证 component 字段确实被绑定。
    seen: list[str] = []
    logger.add(seen.append, format="{extra[component]} {message}")
    get_logger("config").info("component-token")
    logger.remove()
    assert [line.strip() for line in seen] == ["config component-token"]
