"""K 线、逐笔和买卖一价的字段检查。不连接 ClickHouse。"""

from __future__ import annotations

import math
from datetime import datetime

from dataprice.contracts import BAR_FREQS, Bar, BookQuote, TakerFlow, Tick, require_name
from dataprice.timeutil import ensure_aware


def validate_bar(bar: Bar) -> Bar:
    require_name(bar.symbol, "标的")
    if bar.freq not in BAR_FREQS:
        raise ValueError(f"不支持的周期: {bar.freq}")
    _aware(bar.exchange_ts, "exchange_ts")
    _aware(bar.local_recv_ts, "local_recv_ts")
    if bar.high < bar.low:
        raise ValueError("high 小于 low")
    if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
        raise ValueError("open/close 超出 high/low")
    if bar.volume < 0 or bar.amount < 0:
        raise ValueError("volume/amount 不能为负")
    return bar


def validate_tick(tick: Tick) -> Tick:
    require_name(tick.symbol, "标的")
    if not tick.trade_id:
        raise ValueError("trade_id 不能为空")
    if tick.side not in ("buy", "sell", "unknown"):
        raise ValueError(f"不支持的方向: {tick.side}")
    _aware(tick.exchange_ts, "exchange_ts")
    _aware(tick.local_recv_ts, "local_recv_ts")
    if tick.price <= 0 or tick.volume < 0:
        raise ValueError("price/volume 不合法")
    return tick


def validate_quote(quote: BookQuote) -> BookQuote:
    require_name(quote.symbol, "标的")
    require_name(quote.source, "来源")
    _aware(quote.exchange_ts, "exchange_ts")
    _aware(quote.local_recv_ts, "local_recv_ts")
    if not (math.isfinite(quote.bid) and math.isfinite(quote.ask)):
        raise ValueError("bid/ask 必须是有限数")
    if not (quote.ask >= quote.bid > 0):
        raise ValueError("ask 必须大于等于 bid，且 bid 大于 0")
    return quote


def validate_taker_flow(row: TakerFlow) -> TakerFlow:
    require_name(row.symbol, "标的")
    require_name(row.source, "来源")
    if row.freq not in BAR_FREQS:
        raise ValueError(f"不支持的周期: {row.freq}")
    _aware(row.exchange_ts, "exchange_ts")
    _aware(row.local_recv_ts, "local_recv_ts")
    for name, value in (
        ("buy_volume", row.buy_volume),
        ("sell_volume", row.sell_volume),
        ("volume", row.volume),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须是有限非负数")
    if row.trade_count < 0:
        raise ValueError("trade_count 不能为负")
    return row


def _aware(value: datetime, label: str) -> None:
    try:
        ensure_aware(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须带时区") from exc
