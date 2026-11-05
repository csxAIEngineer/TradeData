"""4H 扫流动性后收回。

做多：这根的最低价刺破前 N 根（不含自己）的最低价，收盘又回到那条低点之上。
做空对称。同一根两边都扫到就放弃。止损在扫单影线之外；缓冲为 0 时，止损就是影线极值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from dataprice.contracts import Side
from dataprice.strategy.candles import Candle


@dataclass(frozen=True)
class Sweep:
    side: Side
    level: float
    wick: float


def find_sweep(bars: Sequence[Candle], index: int, n: int) -> Sweep | None:
    """前 ``n`` 根定摆动点。信号这根不放进窗口。"""
    if n < 2:
        raise ValueError("swing_n 至少为 2")
    if index < n or index >= len(bars):
        return None
    window = bars[index - n : index]
    bar = bars[index]
    swing_low = min(item.low for item in window)
    swing_high = max(item.high for item in window)
    long_hit = bar.low < swing_low and bar.close > swing_low
    short_hit = bar.high > swing_high and bar.close < swing_high
    if long_hit and short_hit:
        return None
    if long_hit:
        return Sweep("buy", swing_low, bar.low)
    if short_hit:
        return Sweep("sell", swing_high, bar.high)
    return None


def stop_beyond_wick(sweep: Sweep, buffer_bps: float) -> float:
    """缓冲把止损再往影线外面推一截。0 就是影线本身。"""
    if buffer_bps < 0:
        raise ValueError("stop_buffer_bps 不能为负")
    if buffer_bps == 0:
        return sweep.wick
    shift = buffer_bps / 10_000.0
    if sweep.side == "buy":
        return sweep.wick * (1.0 - shift)
    return sweep.wick * (1.0 + shift)
