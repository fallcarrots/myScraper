"""cli 模块与包元数据测试：占位入口与 __version__ / __all__。"""

from __future__ import annotations

from pathlib import Path

import myscraper
from myscraper.cli import main


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
