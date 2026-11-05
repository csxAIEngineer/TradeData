"""日线偏向。只在已经收完的 UTC 日上计算，下一会话才能用。

做多：HH/HL，或者收盘站上前 ``structure_days`` 根日线的最高价，并且收盘在 EMA 之上。
做空对称。两边同时成立，或者条件不齐，就是平。没有偏向就不开仓。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Sequence

from dataprice.strategy.candles import Candle, assert_sorted
from dataprice.strategy.sniper.params import SniperParams
from dataprice.timeutil import UTC

BiasSide = Literal["long", "short", "flat"]


@dataclass(frozen=True)
class DailyBias:
    """``day`` 是这根日线的 UTC 日期。它的偏向从下一个 UTC 日开始生效。"""

    day: date
    bias: BiasSide
    close: float
    ema: float | None
    hh_hl: bool
    lh_ll: bool
    above_prior_high: bool
    below_prior_low: bool


def ema_close(closes: Sequence[float], span: int) -> list[float | None]:
    """收盘的 EMA。前 ``span`` 根的 SMA 做种子，之后 α = 2 / (span + 1)。"""
    if span < 2:
        raise ValueError("ema_span 至少为 2")
    out: list[float | None] = [None] * len(closes)
    if len(closes) < span:
        return out
    seed = sum(closes[:span]) / span
    out[span - 1] = seed
    alpha = 2.0 / (span + 1.0)
    prev = seed
    for index in range(span, len(closes)):
        prev = alpha * closes[index] + (1.0 - alpha) * prev
        out[index] = prev
    return out


def daily_bias(bars: Sequence[Candle], params: SniperParams | None = None) -> tuple[DailyBias, ...]:
    """一根 UTC 日一根。枢轴要等右侧 ``pivot_k`` 根收完才算数。"""
    params = params or SniperParams()
    assert_sorted(bars)
    dates = _dates(bars)
    closes = [bar.close for bar in bars]
    averages = ema_close(closes, params.ema_span)
    swing_highs, swing_lows = _swings(bars, params.pivot_k)
    rows: list[DailyBias] = []
    for index, bar in enumerate(bars):
        known_highs = [pivot for pivot in swing_highs if pivot + params.pivot_k <= index]
        known_lows = [pivot for pivot in swing_lows if pivot + params.pivot_k <= index]
        hh_hl = _higher(bars, known_highs, "high") and _higher(bars, known_lows, "low")
        lh_ll = _lower(bars, known_highs, "high") and _lower(bars, known_lows, "low")
        above = False
        below = False
        if index >= params.structure_days:
            prior = bars[index - params.structure_days : index]
            above = bar.close > max(item.high for item in prior)
            below = bar.close < min(item.low for item in prior)
        ema = averages[index]
        bias: BiasSide = "flat"
        if ema is not None:
            long_ok = (hh_hl or above) and bar.close > ema
            short_ok = (lh_ll or below) and bar.close < ema
            if long_ok and not short_ok:
                bias = "long"
            elif short_ok and not long_ok:
                bias = "short"
        rows.append(
            DailyBias(
                day=dates[index],
                bias=bias,
                close=bar.close,
                ema=ema,
                hh_hl=hh_hl,
                lh_ll=lh_ll,
                above_prior_high=above,
                below_prior_low=below,
            )
        )
    return tuple(rows)


def bias_for_session(rows: Sequence[DailyBias], session: date) -> DailyBias | None:
    """会话日只用更早的日线。当天的日线收盘还不能用。"""
    chosen: DailyBias | None = None
    for row in rows:
        if row.day >= session:
            break
        chosen = row
    return chosen


def _dates(bars: Sequence[Candle]) -> list[date]:
    dates: list[date] = []
    for bar in bars:
        day = bar.open_ts.astimezone(UTC).date()
        if dates and day <= dates[-1]:
            raise ValueError("日线必须按 UTC 日严格递增，一天一根")
        dates.append(day)
    return dates


def _swings(bars: Sequence[Candle], k: int) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    for index in range(k, len(bars) - k):
        high = bars[index].high
        low = bars[index].low
        left = bars[index - k : index]
        right = bars[index + 1 : index + 1 + k]
        if all(high > bar.high for bar in left) and all(high > bar.high for bar in right):
            highs.append(index)
        if all(low < bar.low for bar in left) and all(low < bar.low for bar in right):
            lows.append(index)
    return highs, lows


def _higher(bars: Sequence[Candle], pivots: Sequence[int], field: str) -> bool:
    if len(pivots) < 2:
        return False
    return getattr(bars[pivots[-1]], field) > getattr(bars[pivots[-2]], field)


def _lower(bars: Sequence[Candle], pivots: Sequence[int], field: str) -> bool:
    if len(pivots) < 2:
        return False
    return getattr(bars[pivots[-1]], field) < getattr(bars[pivots[-2]], field)
