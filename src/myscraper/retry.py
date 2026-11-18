"""重试决策纯逻辑：是否重试、Retry-After 解析、指数退避计算。

纯函数模块，不依赖 httpx、不执行 sleep——等待动作由调用方通过注入的
`Clock` 协议完成（见 `myscraper.clock`），以保证可测试、可替换。
"""

import email.utils
from datetime import UTC, datetime
from typing import Final

__all__ = [
    "IDEMPOTENT",
    "RetryPolicy",
    "backoff_delay",
    "parse_retry_after",
    "should_retry",
]

#: 视为幂等、允许自动重试的 HTTP 方法集合。
IDEMPOTENT: Final[frozenset[str]] = frozenset({"GET", "HEAD"})


def should_retry(
    method: str,
    *,
    status_code: int | None,
    attempt: int,
    exc: BaseException | None,
    max_retries: int,
) -> bool:
    """判断一次失败请求是否应重试。

    决策顺序（短路求值）：
      1. 非幂等方法（非 GET/HEAD）→ 不重试；
      2. `attempt > max_retries` → 已达上限，不重试；
      3. 403 → 禁止类错误，不重试；
      4. 其他非 429 的 4xx → 客户端错误，不重试；
      5. 429 或 5xx → 可重试；
      6. 无状态码但有异常（网络层失败）→ 可重试；
      7. 其余情况 → 不重试。
    """
    if method.upper() not in IDEMPOTENT:
        return False
    if attempt > max_retries:
        return False
    if status_code is None:
        return exc is not None
    if status_code == 403:
        return False
    if 400 <= status_code < 500 and status_code != 429:
        return False
    return status_code == 429 or 500 <= status_code < 600


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """解析 Retry-After 头，返回应等待的秒数。

    - `value` 为 None/空白 → None（无指示，交由退避策略）；
    - 纯数字（delta-seconds）→ 该数值，clamp 到 >= 0；
    - 否则按 HTTP-date 解析（`email.utils.parsedate_to_datetime`），
      naive 结果补 UTC，返回 `max(0.0, (dt - now).total_seconds())`；
    - 解析失败或非法输入 → None。
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds)
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    elapsed: float = max(0.0, float((dt - now).total_seconds()))
    return elapsed


def backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """计算第 attempt 次的指数退避延迟（秒）。

    `attempt` 先 clamp 到 >= 1，返回 `min(base * 2 ** (attempt - 1), cap)`。
    """
    steps: int = max(1, attempt) - 1
    delay: float = base * float(2**steps)
    return min(delay, cap)


class RetryPolicy:
    """聚合重试参数的策略对象：优先遵循 Retry-After，否则用指数退避。"""

    def __init__(self, *, max_retries: int, base_backoff_s: float, max_backoff_s: float) -> None:
        """初始化策略。

        Args:
            max_retries: 最大重试次数（不含首次请求）。
            base_backoff_s: 指数退避基数（秒）。
            max_backoff_s: 退避上限（秒），同时用于 clamp Retry-After。
        """
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s

    def delay_for(self, attempt: int, *, retry_after_s: float | None) -> float:
        """计算第 attempt 次重试前应等待的秒数。

        `retry_after_s` 非 None 且 >= 0 时返回 `min(retry_after_s, max_backoff_s)`
        （服务端指示优先，但仍受上限约束）；否则退回 `backoff_delay` 指数退避。
        """
        if retry_after_s is not None and retry_after_s >= 0:
            return min(retry_after_s, self.max_backoff_s)
        return backoff_delay(attempt, base=self.base_backoff_s, cap=self.max_backoff_s)
