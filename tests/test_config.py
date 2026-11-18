"""config 模块测试：默认值 / respect_robots 强制开启 / 并发边界 / UA / YAML 与环境变量加载。"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from myscraper.config import (
    AppConfig,
    build_user_agent,
    load_config,
    load_yaml_file,
)
from myscraper.errors import ConfigError

# --- 默认值 ---


def test_default_values() -> None:
    """默认配置：max_concurrency=8、respect_robots=True、max_retries=3、format=jsonl。"""
    cfg = AppConfig()
    assert cfg.max_concurrency == 8
    assert cfg.robots.respect_robots is True
    assert cfg.retry.max_retries == 3
    assert cfg.output.format == "jsonl"


# --- respect_robots 强制开启 ---


@pytest.mark.parametrize("value", [False, 0, "false", "no", "on", "1", None])
def test_respect_robots_cannot_be_disabled(value: Any) -> None:
    """任何非 True 取值均抛 ValidationError。"""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"robots": {"respect_robots": value}})


def test_respect_robots_true_passes() -> None:
    """显式 True 通过校验。"""
    cfg = AppConfig.model_validate({"robots": {"respect_robots": True}})
    assert cfg.robots.respect_robots is True


# --- max_concurrency 边界 ---


@pytest.mark.parametrize("value", [65, 0])
def test_max_concurrency_out_of_range(value: int) -> None:
    """超出 [1, 64] 范围抛 ValidationError。"""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"max_concurrency": value})


# --- build_user_agent ---


def test_build_user_agent() -> None:
    """UA 字符串形如 myscraper/<version> (+<url>)。"""
    assert (
        build_user_agent("0.1.0", "https://example.com") == "myscraper/0.1.0 (+https://example.com)"
    )


# --- YAML 加载 ---


def test_load_yaml_file_parses_no_as_string(tmp_path: Path) -> None:
    """严格 Loader 下 YAML 1.1 隐式布尔 no 解析为字符串。"""
    path = tmp_path / "config.yaml"
    path.write_text("robots:\n  respect_robots: no\n", encoding="utf-8")
    data = load_yaml_file(path)
    nested = data["robots"]
    assert isinstance(nested, dict)
    assert nested["respect_robots"] == "no"


def test_load_config_rejects_string_no(tmp_path: Path) -> None:
    """字符串 "no" 无法通过 respect_robots 校验，抛 ConfigError。"""
    path = tmp_path / "config.yaml"
    path.write_text("robots:\n  respect_robots: no\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# --- 环境变量 ---


def test_env_max_concurrency() -> None:
    """白名单环境变量正确覆盖默认值。"""
    cfg = load_config(env={"MYSCRAPER_MAX_CONCURRENCY": "4"})
    assert cfg.max_concurrency == 4


def test_env_respect_robots_rejected() -> None:
    """respect_robots 不可通过环境变量设置。"""
    with pytest.raises(ConfigError):
        load_config(env={"MYSCRAPER_RESPECT_ROBOTS": "1"})


def test_env_unknown_key_rejected() -> None:
    """非白名单环境变量抛 ConfigError。"""
    with pytest.raises(ConfigError):
        load_config(env={"MYSCRAPER_UNKNOWN_KEY": "1"})


def test_env_overrides_yaml(tmp_path: Path) -> None:
    """环境变量优先级高于 YAML 文件取值。"""
    path = tmp_path / "config.yaml"
    path.write_text("max_concurrency: 4\n", encoding="utf-8")
    cfg = load_config(path, env={"MYSCRAPER_MAX_CONCURRENCY": "16"})
    assert cfg.max_concurrency == 16


def test_missing_path_raises(tmp_path: Path) -> None:
    """不存在的配置文件路径抛 ConfigError。"""
    with pytest.raises(ConfigError):
        load_config(tmp_path / "no-such-file.yaml")
