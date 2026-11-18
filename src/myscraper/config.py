"""运行配置模型（Pydantic v2）。

合规红线：
1. `RobotsSettings.respect_robots` 只能为 True——不提供任何关闭 robots.txt
   检查的配置入口（含配置文件与环境变量），双层校验强制拦截。
2. 所有模型禁止 headers / cookies / user_agent 字段，防止通过配置注入
   伪造身份或绕过站点识别的请求头。
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError

#: 默认并发数（低并发设计，降低对目标站点的压力）。
DEFAULT_MAX_CONCURRENCY: int = 8
#: 并发数硬上限，任何配置不得超越。
MAX_CONCURRENCY_HARD_CAP: int = 64
#: 每主机默认速率限制（请求/秒）。
DEFAULT_RATE_LIMIT_PER_HOST: float = 1.0
#: 同主机两次请求之间的最小间隔秒数。
DEFAULT_MIN_REQUEST_INTERVAL: float = 0.5


class HttpSettings(BaseModel):
    """HTTP 请求超时与响应体大小限制。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: TCP 连接超时秒数。
    connect_timeout_s: float = Field(default=10.0, gt=0)
    #: 读取响应超时秒数。
    read_timeout_s: float = Field(default=30.0, gt=0)
    #: 单个 HTML 响应体最大字节数（默认 5 MiB）。
    max_html_bytes: int = Field(default=5242880, ge=1)


class RateLimitSettings(BaseModel):
    """每主机速率限制设置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 每主机速率限制（请求/秒）。
    rate_limit_per_host: float = Field(default=DEFAULT_RATE_LIMIT_PER_HOST, gt=0)
    #: 同主机两次请求之间的最小间隔秒数。
    min_request_interval: float = Field(default=DEFAULT_MIN_REQUEST_INTERVAL, ge=0)


class RetrySettings(BaseModel):
    """失败重试与指数退避设置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 最大重试次数（含首次请求后的额外尝试）。
    max_retries: int = Field(default=3, ge=0, le=10)
    #: 指数退避基准秒数。
    base_backoff_s: float = Field(default=0.5, gt=0)
    #: 单次退避上限秒数。
    max_backoff_s: float = Field(default=30.0, gt=0)


class RobotsSettings(BaseModel):
    """robots.txt 合规设置。

    `respect_robots` 不可通过配置文件或环境变量关闭，双层校验强制其为 True。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 是否遵守 robots.txt（恒为 True，禁止关闭）。
    respect_robots: bool = True
    #: 愿意等待的最大 crawl-delay 秒数，超过则跳过该站点。
    max_crawl_delay_s: float = Field(default=30.0, ge=0)
    #: robots.txt 解析结果缓存 TTL 秒数。
    cache_ttl_s: float = Field(default=3600.0, ge=0)

    @field_validator("respect_robots", mode="before")
    @classmethod
    def _respect_robots_cannot_be_disabled(cls, v: object) -> bool:
        """强制 respect_robots 只接受 True，任何其他取值直接拒绝。"""
        if v is not True:
            raise ValueError(
                "respect_robots 不可通过配置文件或环境变量关闭；"
                "本工具不提供任何关闭 robots 检查的入口"
            )
        return True

    @model_validator(mode="after")
    def _assert_respect_robots(self) -> Self:
        """模型级二次断言，确保 respect_robots 恒为 True。"""
        if self.respect_robots is not True:
            raise ValueError(
                "respect_robots 不可通过配置文件或环境变量关闭；"
                "本工具不提供任何关闭 robots 检查的入口"
            )
        return self


class ExtractSettings(BaseModel):
    """页面抽取限制设置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 单页抽取文本的最大字符数。
    max_text_chars: int = Field(default=20000, ge=1)
    #: 单页抽取链接的最大数量。
    max_links: int = Field(default=500, ge=0)
    #: 自定义 CSS 选择器映射（键为语义名，值为选择器表达式）。
    selectors: dict[str, object] = Field(default_factory=dict)


class OutputSettings(BaseModel):
    """输出格式设置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 输出格式：jsonl / json / csv。
    format: Literal["jsonl", "json", "csv"] = "jsonl"
    #: CSV 输出是否带 UTF-8 BOM（便于 Excel 打开）。
    csv_utf8_bom: bool = False


class AppConfig(BaseModel):
    """应用总配置，聚合各子配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 全局最大并发数（受 MAX_CONCURRENCY_HARD_CAP 约束）。
    max_concurrency: int = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1, le=MAX_CONCURRENCY_HARD_CAP)
    #: HTTP 超时与体积限制。
    http: HttpSettings = Field(default_factory=HttpSettings)
    #: 每主机速率限制。
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    #: 重试与退避。
    retry: RetrySettings = Field(default_factory=RetrySettings)
    #: robots.txt 合规（不可关闭）。
    robots: RobotsSettings = Field(default_factory=RobotsSettings)
    #: 页面抽取限制。
    extract: ExtractSettings = Field(default_factory=ExtractSettings)
    #: 输出格式。
    output: OutputSettings = Field(default_factory=OutputSettings)


def default_config() -> AppConfig:
    """返回默认应用配置。"""
    return AppConfig()


def build_user_agent(version: str, project_url: str) -> str:
    """构造符合规范的 User-Agent 标识字符串。"""
    return f"myscraper/{version} (+{project_url})"


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MIN_REQUEST_INTERVAL",
    "DEFAULT_RATE_LIMIT_PER_HOST",
    "ENV_MAP",
    "ENV_PREFIX",
    "MAX_CONCURRENCY_HARD_CAP",
    "AppConfig",
    "ExtractSettings",
    "HttpSettings",
    "OutputSettings",
    "RateLimitSettings",
    "RetrySettings",
    "RobotsSettings",
    "build_user_agent",
    "default_config",
    "load_config",
    "load_yaml_file",
]


class _StrictSafeLoader(yaml.SafeLoader):
    """禁用 YAML 1.1 隐式布尔（yes/no/on/off），只认 true/false 六种字面量。"""


_StrictSafeLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_yaml_file(path: Path) -> dict[str, object]:
    """读取并解析 YAML 配置文件；失败一律抛 ConfigError。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件: {path}") from exc
    try:
        data = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 格式错误: {path}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射: {path}")
    return data


#: 环境变量统一前缀。
ENV_PREFIX: Final[str] = "MYSCRAPER_"

#: 环境变量白名单：环境变量名 -> (配置点路径, 取值类型)。
ENV_MAP: Final[dict[str, tuple[str, type[int] | type[float] | type[str]]]] = {
    "MYSCRAPER_MAX_CONCURRENCY": ("max_concurrency", int),
    "MYSCRAPER_RATE_LIMIT_PER_HOST": ("rate_limit.rate_limit_per_host", float),
    "MYSCRAPER_MIN_REQUEST_INTERVAL": ("rate_limit.min_request_interval", float),
    "MYSCRAPER_CONNECT_TIMEOUT_S": ("http.connect_timeout_s", float),
    "MYSCRAPER_READ_TIMEOUT_S": ("http.read_timeout_s", float),
    "MYSCRAPER_MAX_HTML_BYTES": ("http.max_html_bytes", int),
    "MYSCRAPER_MAX_RETRIES": ("retry.max_retries", int),
    "MYSCRAPER_BASE_BACKOFF_S": ("retry.base_backoff_s", float),
    "MYSCRAPER_MAX_BACKOFF_S": ("retry.max_backoff_s", float),
    "MYSCRAPER_MAX_TEXT_CHARS": ("extract.max_text_chars", int),
    "MYSCRAPER_MAX_LINKS": ("extract.max_links", int),
    "MYSCRAPER_CACHE_TTL_S": ("robots.cache_ttl_s", float),
    "MYSCRAPER_MAX_CRAWL_DELAY_S": ("robots.max_crawl_delay_s", float),
}


def _deep_set(target: dict[str, object], dotted: str, value: object) -> None:
    """按点分路径逐层建 dict 并在叶子处赋值。"""
    keys = dotted.split(".")
    node: dict[str, object] = target
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


def load_config(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> AppConfig:
    """加载并校验应用配置，优先级：YAML < ENV（环境变量覆盖文件取值）。

    Args:
        path: YAML 配置文件路径；None 表示不读取文件（仅默认值 + 环境变量）。
        env: 环境变量映射；None 时使用 ``os.environ``。

    Returns:
        校验通过后的 AppConfig 实例。

    Raises:
        ConfigError: 环境变量不在白名单、取值非法，或整体校验失败。
    """
    data: dict[str, object] = load_yaml_file(path) if path is not None else {}
    env_source: Mapping[str, str] = os.environ if env is None else env
    for key, raw in env_source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        if key == "MYSCRAPER_RESPECT_ROBOTS":
            raise ConfigError(
                "respect_robots 不可通过环境变量设置；本工具不提供任何关闭 robots 检查的入口"
            )
        if key not in ENV_MAP:
            raise ConfigError(f"未知环境变量: {key}")
        dotted, value_type = ENV_MAP[key]
        try:
            value = value_type(raw)
        except ValueError:
            raise ConfigError(f"环境变量取值非法: {key}") from None
        _deep_set(data, dotted, value)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"配置校验失败: {exc.error_count()} 处错误") from exc
