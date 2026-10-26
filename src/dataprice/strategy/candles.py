"""策略用的 K 线。只有价格和时间，不连数据库。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

import pandas as pd

from dataprice.contracts import Bar
from dataprice.timeutil import UTC, ensure_aware

FREQ_MINUTES = {"30m": 30, "60m": 60, "4h": 240}


def bar_delta(freq: str) -> timedelta:
    try:
        minutes = FREQ_MINUTES[freq]
    except KeyError as exc:
        raise ValueError(f"策略周期只支持 30m、60m 和 4h，收到 {freq}") from exc
    return timedelta(minutes=minutes)


def aggregate_candles(candles: Sequence[Candle], freq: str) -> list[Candle]:
    """把更细的 K 线收成 ``freq``。没走完的最后一根不输出，避免用未收盘价格。"""
    width = bar_delta(freq)
    hours = int(width.total_seconds() // 3600)
    if hours < 1:
        raise ValueError("只能往上合成整小时周期")
    buckets: dict[datetime, list[Candle]] = {}
    order: list[datetime] = []
    for candle in candles:
        ts = candle.open_ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        key = ts.replace(hour=(ts.hour // hours) * hours)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(candle)
    out: list[Candle] = []
    for key in order:
        group = buckets[key]
        last = group[-1].open_ts.astimezone(UTC)
        if last < key + width - timedelta(hours=1):
            continue
        out.append(
            Candle(
                open_ts=key,
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum(item.volume for item in group),
            )
        )
    return out


@dataclass(frozen=True)
class Candle:
    """``open_ts`` 是这根 K 线的开盘时间，必须带时区。"""

    open_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        ensure_aware(self.open_ts)
        if self.high < self.low:
            raise ValueError("high 小于 low")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("open/close 超出 high/low")
        if self.volume < 0:
            raise ValueError("volume 不能为负")

    def close_ts(self, freq: str) -> datetime:
        return self.open_ts + bar_delta(freq)


def candles_from_bars(bars: list[Bar] | tuple[Bar, ...]) -> list[Candle]:
    return [
        Candle(
            open_ts=bar.exchange_ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in bars
    ]


def candles_from_frame(frame: pd.DataFrame) -> list[Candle]:
    """读 ``market.bars`` 查询结果。需要 exchange_ts、open、high、low、close。"""
    if frame.empty:
        return []
    missing = {"exchange_ts", "open", "high", "low", "close"} - set(frame.columns)
    if missing:
        raise ValueError(f"K 线缺少列: {sorted(missing)}")
    volume_col = "volume" if "volume" in frame.columns else None
    candles: list[Candle] = []
    for row in frame.itertuples(index=False):
        ts = row.exchange_ts
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        candles.append(
            Candle(
                open_ts=ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(getattr(row, volume_col)) if volume_col else 0.0,
            )
        )
    return candles


def assert_sorted(candles: list[Candle] | tuple[Candle, ...]) -> None:
    for prev, cur in zip(candles, candles[1:]):
        if cur.open_ts <= prev.open_ts:
            raise ValueError("K 线必须按 open_ts 严格升序")
