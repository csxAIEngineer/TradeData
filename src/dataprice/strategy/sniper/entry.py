"""4H 多 EMA 转势，并且波动不高于自己近期的 30 分位。

做多：EMA6、EMA12、EMA32 每一条都严格高于 ``ema_slope_bars`` 根之前（默认就是上一根）。
做空三条都严格更低。走平不算。不要求快线在慢线之上。
ATR 用 Wilder。低波动看 ATR/收盘，不高于最近 ``atr_percentile_window`` 根（含本根）的
``atr_percentile`` 分位，默认 0.30。
止损在这根影线之外。缓冲为 0 时，做多止损是最低价，做空是最高价。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from dataprice.contracts import Side
from dataprice.strategy.candles import Candle
from dataprice.strategy.sniper.bias import ema_close
from dataprice.strategy.sniper.params import SniperParams


@dataclass(frozen=True)
class EntrySeries:
    """一根 4H 一套数。不够长的位置是 None。"""

    spans: tuple[int, int, int]
    emas: Mapping[int, Sequence[float | None]]
    atr: Sequence[float | None]
    ratios: Sequence[float | None]


@dataclass(frozen=True)
class EmaSignal:
    side: Side
    atr: float
    atr_ratio: float
    threshold_ratio: float
    wick: float


def entry_series(bars: Sequence[Candle], params: SniperParams) -> EntrySeries:
    """EMA 和 ATR 整段只算一次。信号函数按根来读。"""
    closes = [bar.close for bar in bars]
    spans = (params.ema_fast, params.ema_mid, params.ema_slow)
    emas = {span: ema_close(closes, span) for span in spans}
    atr = wilder_atr(bars, params.atr_length)
    ratios: list[float | None] = []
    for bar, value in zip(bars, atr):
        if value is None or bar.close <= 0:
            ratios.append(None)
        else:
            ratios.append(value / bar.close)
    return EntrySeries(spans=spans, emas=emas, atr=atr, ratios=ratios)


def find_ema_turn(
    bars: Sequence[Candle],
    index: int,
    params: SniperParams,
    series: EntrySeries | None = None,
) -> EmaSignal | None:
    """这根 4H 三条 EMA 同向转，并且 ATR/收盘不高于自己的滚动分位。"""
    if index < 0 or index >= len(bars):
        return None
    series = series or entry_series(bars, params)
    lookback = params.ema_slope_bars
    up = all(_slope(series.emas[span], index, lookback, up=True) for span in series.spans)
    down = all(_slope(series.emas[span], index, lookback, up=False) for span in series.spans)
    if up:
        side: Side = "buy"
    elif down:
        side = "sell"
    else:
        return None
    sample = _ratio_window(series.ratios, index, params.atr_percentile_window)
    if sample is None:
        return None
    current = sample[-1]
    threshold = inclusive_percentile(sample, params.atr_percentile)
    if current > threshold:
        return None
    atr_now = series.atr[index]
    if atr_now is None:
        return None
    bar = bars[index]
    return EmaSignal(
        side=side,
        atr=atr_now,
        atr_ratio=current,
        threshold_ratio=threshold,
        wick=bar.low if side == "buy" else bar.high,
    )


def wilder_atr(bars: Sequence[Candle], length: int) -> list[float | None]:
    """Wilder ATR。前 ``length`` 根真实波幅的平均做种子，之后 (前值 × (n-1) + TR) / n。

    第一根没有上一根收盘，真实波幅就是最高价减最低价。
    """
    if length < 2:
        raise ValueError("atr_length 至少为 2")
    out: list[float | None] = [None] * len(bars)
    if len(bars) < length:
        return out
    true_ranges: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        if prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        true_ranges.append(tr)
        prev_close = bar.close
    seed = sum(true_ranges[:length]) / length
    out[length - 1] = seed
    prev = seed
    for index in range(length, len(bars)):
        prev = (prev * (length - 1) + true_ranges[index]) / length
        out[index] = prev
    return out


def stop_beyond_wick(side: Side, wick: float, buffer_bps: float) -> float:
    """缓冲把止损再往影线外面推一截。0 就是影线本身。"""
    if buffer_bps < 0:
        raise ValueError("stop_buffer_bps 不能为负")
    if side not in ("buy", "sell"):
        raise ValueError("side 只能是 buy 或 sell")
    if buffer_bps == 0:
        return wick
    shift = buffer_bps / 10_000.0
    if side == "buy":
        return wick * (1.0 - shift)
    return wick * (1.0 + shift)


def _slope(values: Sequence[float | None], index: int, lookback: int, *, up: bool) -> bool:
    if lookback < 1 or index < lookback:
        return False
    now = values[index]
    prev = values[index - lookback]
    if now is None or prev is None:
        return False
    if up:
        return now > prev
    return now < prev


def inclusive_percentile(sample: Sequence[float], percentile: float) -> float:
    """含端点的线性分位。位置是 ``percentile * (n - 1)``，落在两档中间就插值。

    和 NumPy 默认的 ``percentile`` 相同。``0.30`` 是第 30 分位，不是中位数。
    """
    if not sample:
        raise ValueError("分位窗口是空的")
    if not 0 < percentile <= 1:
        raise ValueError("atr_percentile 必须在 (0, 1] 内")
    ordered = sorted(sample)
    if len(ordered) == 1:
        return ordered[0]
    position = percentile * (len(ordered) - 1)
    lower = int(position)
    if lower >= len(ordered) - 1:
        return ordered[-1]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[lower + 1] * weight


def _ratio_window(ratios: Sequence[float | None], index: int, window: int) -> list[float] | None:
    start = index - window + 1
    if start < 0:
        return None
    sample: list[float] = []
    for cursor in range(start, index + 1):
        value = ratios[cursor]
        if value is None:
            return None
        sample.append(value)
    return sample
