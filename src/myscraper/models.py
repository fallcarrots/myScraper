"""抓取结果数据模型（Pydantic v2）。

合规红线：默认数据模型**不预设任何 PII 字段**——禁止出现 author / email /
phone / ip / user_id 等个人身份信息字段；如业务确需采集，必须由使用方在
合规评审通过后显式扩展子模型，且不得修改本模块的默认字段集。
"""

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 单条抓取记录的处理结果状态。
RecordStatus = Literal["ok", "skipped", "failed"]
#: 页面级状态别名（与 RecordStatus 同构，供页面处理层语义化引用）。
PageStatus = RecordStatus


class PageRecord(BaseModel):
    """单次页面抓取的不可变记录。

    frozen + extra="forbid"：实例不可变，且禁止注入模型未声明（尤其 PII）的字段。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 请求的初始 URL（未经重定向）。
    url: str
    #: 重定向链结束后的最终 URL。
    final_url: str
    #: HTTP 响应状态码（如 200 / 404 / 503）。
    status_code: int
    #: <title> 提取结果，解析失败或缺失时为 None。
    title: str | None = None
    #: 提取出的正文纯文本，失败时为空字符串。
    text: str = ""
    #: 页面上发现的超链接绝对 URL 列表。
    links: list[str] = Field(default_factory=list)
    #: 抓取完成时间；必须为 tz-aware（UTC），见下方校验器。
    fetched_at: datetime
    #: 本条记录的总体处理状态，默认 "ok"。
    status: RecordStatus = "ok"
    #: 当 status == "skipped" 时的跳过原因（如 robots.txt 禁止）。
    skip_reason: str | None = None
    #: 当 status == "failed" 时的错误摘要。
    error: str | None = None
    #: 合规元数据（如 robots、许可证、来源声明），键值均为字符串。
    compliance: dict[str, str] = Field(default_factory=dict)

    @field_validator("fetched_at")
    @classmethod
    def _fetched_at_must_be_tz_aware(cls, dt: datetime) -> datetime:
        """校验 fetched_at 必须带时区信息（tz-aware，约定为 UTC）。"""
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("fetched_at 必须带时区（tz-aware UTC）")
        return dt


#: PageRecord 的全部字段名，供 output 层生成 CSV 表头。
PAGE_FIELDS: Final[tuple[str, ...]] = tuple(PageRecord.model_fields)

__all__ = ["PAGE_FIELDS", "PageRecord", "PageStatus", "RecordStatus"]
