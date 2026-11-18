"""retry 模块纯逻辑的表驱动参数化测试。

覆盖 should_retry / parse_retry_after / backoff_delay / RetryPolicy。
"""

from datetime import UTC, datetime

import pytest

from myscraper.retry import RetryPolicy, backoff_delay, parse_retry_after, should_retry

#: 测试用固定"当前时刻"，用于 Retry-After 的 HTTP-date 解析。
NOW: datetime = datetime(2026, 10, 21, 7, 27, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("method", "status_code", "attempt", "exc", "expected"),
    [
        ("GET", 403, 1, None, False),  # 禁止类错误不重试
        ("GET", 404, 1, None, False),  # 客户端错误不重试
        ("GET", 429, 1, None, True),  # 限流可重试
        ("GET", 503, 1, None, True),  # 服务端错误可重试
        ("POST", 503, 1, None, False),  # 非幂等方法不重试
        ("GET", 503, 4, None, False),  # 超过最大重试次数不重试
        ("GET", None, 1, TimeoutError(), True),  # 网络层异常可重试
    ],
)
def test_should_retry(
    method: str,
    status_code: int | None,
    attempt: int,
    exc: BaseException | None,
    expected: bool,
) -> None:
    """按决策优先级正确判定是否重试。"""
    assert (
        should_retry(method, status_code=status_code, attempt=attempt, exc=exc, max_retries=3)
        is expected
    )


def test_parse_retry_after_delta_seconds() -> None:
    """纯数字按 delta-seconds 解析。"""
    assert parse_retry_after("7", now=NOW) == 7.0


@pytest.mark.parametrize("value", [None, "garbage"])
def test_parse_retry_after_none_cases(value: str | None) -> None:
    """None / 无法解析的输入返回 None。"""
    assert parse_retry_after(value, now=NOW) is None


def test_parse_retry_after_http_date() -> None:
    """HTTP-date 按 now 差值计算秒数。"""
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=NOW) == 60.0


def test_backoff_delay_exponential() -> None:
    """指数退避：base * 2 ** (attempt - 1)。"""
    assert backoff_delay(1, base=0.5, cap=30.0) == 0.5
    assert backoff_delay(3, base=0.5, cap=30.0) == 2.0


def test_retry_policy_delay_for_prefers_retry_after() -> None:
    """Retry-After 优先于指数退避。"""
    policy = RetryPolicy(max_retries=3, base_backoff_s=0.5, max_backoff_s=30.0)
    assert policy.delay_for(1, retry_after_s=7.0) == 7.0


def test_retry_policy_delay_for_falls_back_to_backoff() -> None:
    """无 Retry-After 时退回指数退避。"""
    policy = RetryPolicy(max_retries=3, base_backoff_s=0.5, max_backoff_s=30.0)
    assert policy.delay_for(1, retry_after_s=None) == 0.5
