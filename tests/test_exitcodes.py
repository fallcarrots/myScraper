"""exitcodes 模块的表驱动参数化测试。"""

from enum import IntEnum

import pytest

from myscraper.exitcodes import ExitCode, decide_exit_code


def test_exit_code_enum_values() -> None:
    """枚举成员与约定取值一致。"""
    assert int(ExitCode.OK) == 0
    assert int(ExitCode.INTERNAL) == 5
    assert int(ExitCode.CONFIG) == 2


@pytest.mark.parametrize(
    ("total", "succeeded", "aborted", "expected"),
    [
        (5, 5, False, 0),  # 全部成功 -> OK
        (5, 2, False, 1),  # 部分成功 -> PARTIAL
        (5, 0, False, 3),  # 全部失败 -> ALL_FAILED
        (0, 0, False, 2),  # 无记录 -> CONFIG
        (5, 3, True, 130),  # 中断 -> 130
        (5, 5, True, 130),  # aborted 优先级最高
        (0, 0, True, 130),  # aborted 优先级最高
    ],
)
def test_decide_exit_code(total: int, succeeded: int, aborted: bool, expected: int) -> None:
    """按判定优先级返回正确退出码。"""
    assert decide_exit_code(total=total, succeeded=succeeded, aborted=aborted) == expected


# 确认枚举基类为 IntEnum
assert isinstance(ExitCode.OK, IntEnum)
