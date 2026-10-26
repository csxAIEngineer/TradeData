"""ICT 失衡：公平价值缺口（FVG）和订单块（OB）。

看多 FVG：第 i 根的最低价高于第 i-2 根的最高价。区间是
``[第 i-2 根最高价, 第 i 根最低价]``，在第 i 根收盘后才算形成。
看空 FVG 相反。

订单块挂在同一段位移上：看多时，从缺口前一根往回找 ``ob_lookback`` 根里
最后一根收阴的 K 线，区间是它的最低价到最高价。没有收阴就不记订单块。
看空则找最后一根收阳。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from dataprice.strategy.candles import Candle

ZoneKind = Literal["fvg", "ob"]
ZoneBias = Literal["bull", "bear"]


@dataclass(frozen=True)
class Zone:
    kind: ZoneKind
    bias: ZoneBias
    index: int
    bottom: float
    top: float


def find_zones(candles: Sequence[Candle], *, ob_lookback: int = 5) -> tuple[Zone, ...]:
    if ob_lookback < 1:
        raise ValueError("ob_lookback 必须 >= 1")
    zones: list[Zone] = []
    for index in range(2, len(candles)):
        left = candles[index - 2]
        right = candles[index]
        if right.low > left.high:
            zones.append(Zone("fvg", "bull", index, left.high, right.low))
            block = _order_block(candles, index, "bull", ob_lookback)
            if block is not None:
                zones.append(block)
        elif right.high < left.low:
            zones.append(Zone("fvg", "bear", index, right.high, left.low))
            block = _order_block(candles, index, "bear", ob_lookback)
            if block is not None:
                zones.append(block)
    return tuple(zones)


def _order_block(
    candles: Sequence[Candle],
    fvg_index: int,
    bias: ZoneBias,
    lookback: int,
) -> Zone | None:
    start = fvg_index - 1
    stop = max(-1, fvg_index - 1 - lookback)
    for cursor in range(start, stop, -1):
        candle = candles[cursor]
        if candle.high <= candle.low:
            continue
        if bias == "bull" and candle.close < candle.open:
            return Zone("ob", "bull", fvg_index, candle.low, candle.high)
        if bias == "bear" and candle.close > candle.open:
            return Zone("ob", "bear", fvg_index, candle.low, candle.high)
    return None
