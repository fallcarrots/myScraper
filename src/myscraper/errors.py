"""项目唯一异常根。

本模块不 import 任何项目内模块，所有自定义异常均以 MyScraperError 为根，
保证上层可以仅凭本模块完成异常捕获与分类。
"""

__all__ = ["ConfigError", "InputError", "MissingRequired", "MyScraperError"]


class MyScraperError(Exception):
    """所有 myScraper 异常的基类。"""


class ConfigError(MyScraperError):
    """配置文件解析或校验失败，对应退出码 2。"""


class InputError(MyScraperError):
    """URL 输入非法或输入文件不存在，对应退出码 2。"""


class MissingRequired(MyScraperError):
    """必填字段未在任何候选选择器下命中。"""

    def __init__(self, slot: str, selector: str) -> None:
        """记录未命中的字段名与对应选择器。

        Args:
            slot: 未命中的字段名。
            selector: 最后一次尝试的选择器表达式。
        """
        self.slot = slot
        self.selector = selector
        super().__init__(f"必填字段未命中: {slot} (selector={selector!r})")
