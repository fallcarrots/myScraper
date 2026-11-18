"""进程退出码定义与汇总判定。

ExitCode 是全项目唯一的退出码枚举，decide_exit_code 按严格优先级
将一次运行的结果汇总为最终退出码。
"""

from enum import IntEnum

__all__ = ["ExitCode", "decide_exit_code"]


class ExitCode(IntEnum):
    """myscraper 的进程退出码。"""

    OK = 0
    PARTIAL = 1
    CONFIG = 2
    ALL_FAILED = 3
    INTERNAL = 5


def decide_exit_code(*, total: int, succeeded: int, aborted: bool = False) -> int:
    """根据一次运行的汇总结果决定进程退出码。

    Args:
        total: 已尝试的记录总数，即 status == "ok" 与 "failed" 之和，不含 skipped。
        succeeded: 成功记录数，只统计 status == "ok" 的记录。
        aborted: 运行是否被用户中断（如 Ctrl+C），为 True 时最高优先级返回 130。

    Returns:
        按优先级从高到低判定：aborted 返回 130；total == 0 返回 CONFIG；
        succeeded 小于等于 0 返回 ALL_FAILED；succeeded 大于等于 total 返回 OK；
        其余情况返回 PARTIAL。
    """
    if aborted:
        return 130
    if total == 0:
        return ExitCode.CONFIG
    if succeeded <= 0:
        return ExitCode.ALL_FAILED
    if succeeded >= total:
        return ExitCode.OK
    return ExitCode.PARTIAL
