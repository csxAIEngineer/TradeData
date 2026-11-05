"""把 30 分钟或 60 分钟收成 UTC 对齐的 4H 和日线。缺一根就不合成那一根。"""

from __future__ import annotations

from datetime import datetime, timedelta

from dataprice.strategy.candles import Candle, assert_sorted
from dataprice.timeutil import UTC

FOUR_HOURS = 240
ONE_DAY = 1440


def aggregate(candles: tuple[Candle, ...] | list[Candle], *, bar_minutes: int, target_minutes: int) -> list[Candle]:
    """``target_minutes`` 是 240 或 1440。桶的起点对齐 UTC 零点。"""
    if bar_minutes < 1 or target_minutes < bar_minutes or target_minutes % bar_minutes != 0:
        raise ValueError("目标周期必须是源周期的整数倍")
    if not candles:
        return []
    assert_sorted(candles)
    need = target_minutes // bar_minutes
    groups: dict[datetime, list[Candle]] = {}
    for bar in candles:
        opened = _minute(bar.open_ts)
        if _minutes_from_midnight(opened) % bar_minutes != 0:
            continue
        key = _floor(opened, target_minutes)
        groups.setdefault(key, []).append(bar)
    out: list[Candle] = []
    for key in sorted(groups):
        bars = groups[key]
        expected = [key + timedelta(minutes=bar_minutes * offset) for offset in range(need)]
        got = [_minute(bar.open_ts) for bar in bars]
        if got != expected:
            continue
        out.append(
            Candle(
                open_ts=key,
                open=bars[0].open,
                high=max(bar.high for bar in bars),
                low=min(bar.low for bar in bars),
                close=bars[-1].close,
                volume=sum(bar.volume for bar in bars),
            )
        )
    return out


def to_4h(candles: tuple[Candle, ...] | list[Candle], *, bar_minutes: int) -> list[Candle]:
    return aggregate(candles, bar_minutes=bar_minutes, target_minutes=FOUR_HOURS)


def to_daily(candles: tuple[Candle, ...] | list[Candle], *, bar_minutes: int) -> list[Candle]:
    return aggregate(candles, bar_minutes=bar_minutes, target_minutes=ONE_DAY)


def _minute(value: datetime) -> datetime:
    stamp = value.astimezone(UTC).replace(second=0, microsecond=0)
    return stamp


def _minutes_from_midnight(stamp: datetime) -> int:
    return stamp.hour * 60 + stamp.minute


def _floor(stamp: datetime, target_minutes: int) -> datetime:
    midnight = stamp.replace(hour=0, minute=0)
    elapsed = _minutes_from_midnight(stamp)
    floored = (elapsed // target_minutes) * target_minutes
    return midnight + timedelta(minutes=floored)
