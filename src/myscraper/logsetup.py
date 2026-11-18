"""日志初始化与组件级 logger 获取。

统一通过 loguru 管理日志：`setup_logging` 负责全局初始化（仅在程序入口调用一次），
其余模块通过 `get_logger(name)` 获取绑定了 component 字段的 logger。

合规约束：
- 绝不添加文件 sink（不落盘缓存 / 不落盘日志）；
- `diagnose=False`，禁止把局部变量（可能含页面内容）打进日志。
"""

from __future__ import annotations

import sys

import loguru

__all__ = ["get_logger", "setup_logging"]


def setup_logging(*, level: str = "INFO", json_mode: bool = False) -> None:
    """初始化全局日志配置，只挂一个 stderr sink。

    必须先 `logger.remove()`：loguru 在 import 时会自动挂一个默认的 stderr sink，
    若不先移除，日志会双份输出。

    Args:
        level: 日志级别，如 "INFO"、"DEBUG"，原样传给 loguru。
        json_mode: True 时 `serialize=True`，输出 JSON 行（便于采集）；
            False 时输出人类可读格式。
    """
    logger = loguru.logger
    # 合规要求：清掉默认 stderr sink，避免双份输出；且绝不添加文件 sink（不落盘）。
    logger.remove()
    # diagnose=False：禁止 loguru 把局部变量值（可能含页面内容等敏感数据）打进日志。
    logger.add(
        sys.stderr,
        level=level,
        serialize=json_mode,
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )


def get_logger(name: str) -> loguru.Logger:
    """获取绑定了 component 字段的 loguru logger。

    Args:
        name: 组件名（如 "fetcher"、"extractor"），会出现在日志的 component 字段中。

    Returns:
        绑定了 `component=name` 的 loguru Logger。
    """
    return loguru.logger.bind(component=name)
