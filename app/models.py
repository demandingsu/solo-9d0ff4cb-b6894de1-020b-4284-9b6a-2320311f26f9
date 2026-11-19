"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_utc(value: str) -> str:
    """ISO 8601 时间 -> 归一化 UTC 字符串 ``YYYY-MM-DDTHH:MM:SS.ffffffZ``。

    固定宽度格式保证字典序即时间序；必须带时区（``Z`` 或偏移），
    朴素时间无法解释，直接拒绝。
    """
    from datetime import datetime, timezone

    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"非法 ISO 8601 时间：{value!r}") from None
    if dt.tzinfo is None:
        raise ValueError("成交时间必须带时区（UTC 时间以 Z 结尾）")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

# ---------- 导入请求 ----------


class QuoteIn(BaseModel):
    """一条日行情。revision 为修订号，同键高修订覆盖低修订。"""

    security_code: str = Field(..., min_length=1, description="证券代码")
    trade_date: str = Field(..., description="交易日，ISO 日期 YYYY-MM-DD")
    revision: int = Field(..., ge=0, description="修订号（非负整数，越高越新）")
    open: float = Field(..., description="开盘价（原值）")
    high: float = Field(..., description="最高价（原值）")
    low: float = Field(..., description="最低价（原值）")
    close: float = Field(..., description="收盘价（原值）")
    volume: float = Field(..., description="成交量（原值）")

    @field_validator("trade_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("价格必须为正数")
        return v

    @field_validator("volume")
    @classmethod
    def _non_negative_volume(cls, v: float) -> float:
        if v < 0:
            raise ValueError("成交量不能为负")
        return v


class QuoteImportRequest(BaseModel):
    quotes: list[QuoteIn] = Field(..., min_length=1)


class CorporateActionIn(BaseModel):
    """一条拆股/并股公告。

    ratio = 每股老股拆成的新股数：1 拆 2 填 2.0，2 并 1 填 0.5。
    """

    security_code: str = Field(..., min_length=1)
    event_no: str = Field(..., min_length=1, description="事件号")
    ex_date: str = Field(..., description="除权日 YYYY-MM-DD")
    ratio: float = Field(..., gt=0, description="比例：老股 1 股 -> 新股 ratio 股")
    revision: int = Field(..., ge=0, description="修订号（非负整数，越高越新）")
    action_type: Literal["split", "reverse_split"] = Field(
        default="split", description="split=拆股，reverse_split=并股"
    )

    @field_validator("ex_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v


class CorporateActionImportRequest(BaseModel):
    actions: list[CorporateActionIn] = Field(..., min_length=1)


# ---------- 逐笔补包请求 ----------


class TradeReportIn(BaseModel):
    """一条成交回报：成交号 + UTC 成交时间 + 价格 + 数量。"""

    trade_no: int = Field(..., ge=0, description="成交号（证券+交易日内唯一）")
    trade_time: str = Field(
        ..., description="UTC 成交时间，ISO 8601 且须带时区（如 2026-01-02T01:05:03Z）"
    )
    price: float = Field(..., gt=0, description="成交价")
    quantity: float = Field(..., gt=0, description="成交量")

    @field_validator("trade_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        return normalize_utc(v)


class RevocationIn(BaseModel):
    """一条撤销回报：只需成交号。撤销是墓碑，先撤后到同样生效。"""

    trade_no: int = Field(..., ge=0, description="被撤销的成交号")


class TradePacketIn(BaseModel):
    """一个补包：同一证券、同一交易日的一批成交/撤销回报，整包原子生效。"""

    security_code: str = Field(..., min_length=1, description="证券代码")
    trade_date: str = Field(..., description="交易日，ISO 日期 YYYY-MM-DD")
    trades: list[TradeReportIn] = Field(default_factory=list, description="成交回报")
    revocations: list[RevocationIn] = Field(
        default_factory=list, description="撤销回报（只需成交号）"
    )

    @field_validator("trade_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _at_least_one_report(self) -> "TradePacketIn":
        if not self.trades and not self.revocations:
            raise ValueError("补包至少要有一条成交或撤销回报")
        return self


# ---------- 导入响应 ----------


class ImportResponse(BaseModel):
    batch_id: int = Field(..., description="本次导入生成的入库批次号")
    payload_kind: Literal["quotes", "corporate_actions", "trade_packet"]
    row_count: int = Field(..., description="实际新写入的行数（完全重复行不计）")
    received: int = Field(..., description="请求中收到的行数")


class TradePacketImportResponse(BaseModel):
    """补包导入结果。row_count = 新写入的逐笔事件数（成交+撤销）。"""

    batch_id: int = Field(..., description="本次补包生成的入库批次号")
    payload_kind: Literal["trade_packet"] = "trade_packet"
    row_count: int = Field(..., description="实际新写入的逐笔事件数（重传不计）")
    received: int = Field(..., description="包内收到的回报总数")
    applied_trades: int = Field(..., description="新写入的成交回报数")
    applied_revocations: int = Field(..., description="新写入的撤销回报数")
    affected_minutes: int = Field(..., description="本次触发重算的分钟数")


# ---------- 查询响应 ----------


class EventBasis(BaseModel):
    """折算依据：对某个行情日实际生效的一条公告。"""

    event_no: str
    ex_date: str
    ratio: float = Field(..., description="公告比例（老 1 股 -> 新 ratio 股）")
    action_type: Literal["split", "reverse_split"]
    revision: int
    ingest_batch_id: int = Field(..., description="该公告所在入库批次")


class AdjustedQuote(BaseModel):
    security_code: str
    trade_date: str
    # 原值（该截止批次下生效修订的原始价量）
    open: float
    high: float
    low: float
    close: float
    volume: float
    # 折算值（以区间末日为基准复权）
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    adjusted_volume: float
    factor: float = Field(..., description="价格折算系数 = 1/各公告比例之积")
    applied_events: list[EventBasis] = Field(..., description="事件依据，按除权日升序")
    cutoff_batch_id: int
    basis_end_date: str = Field(..., description="基准日（= 请求区间末日）")


class RawQuote(BaseModel):
    security_code: str
    trade_date: str
    revision: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ingest_batch_id: int


class CorporateActionOut(BaseModel):
    security_code: str
    event_no: str
    ex_date: str
    ratio: float
    action_type: Literal["split", "reverse_split"]
    revision: int
    ingest_batch_id: int


class BatchOut(BaseModel):
    id: int
    payload_kind: Literal["quotes", "corporate_actions", "trade_packet"]
    row_count: int
    imported_at: str


# ---------- 逐笔汇总查询响应 ----------


class MinuteBar(BaseModel):
    """一分钟的开高低收与成交量（按未撤成交）。"""

    minute_ts: str = Field(..., description="UTC 分钟，YYYY-MM-DDTHH:MM:00Z")
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int = Field(..., description="该分钟未撤成交笔数")


class DailyBar(BaseModel):
    """当日汇总（由分钟线聚合，与分钟线来自同一已提交状态）。"""

    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int


class DailyQuoteImport(BaseModel):
    """可直接提交 ``POST /imports/quotes`` 的日线数据（修订号由调用方指定）。"""

    security_code: str
    trade_date: str
    revision: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarsResponse(BaseModel):
    """一次查询返回的两级汇总，取自同一已提交快照。"""

    security_code: str
    trade_date: str
    minute_bars: list[MinuteBar] = Field(..., description="分钟线，按 UTC 分钟升序")
    daily: DailyBar | None = Field(
        ..., description="当日汇总；无未撤成交时为 null"
    )
    daily_quote_import: DailyQuoteImport | None = Field(
        ..., description="仍有成交时给出，可直接提交日行情导入接口；否则为 null"
    )
