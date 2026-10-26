"""SMC 结构：摆动高低点、BOS、CHoCH。

摆动点要左右各 ``wing`` 根都严格更低（高点）或更高（低点）才算确认。
确认发生在摆动点之后第 ``wing`` 根，计算某一根的偏向时不会看更后面的 K 线。

结构位只用最近一个尚未被收盘突破的摆动高点和摆动低点：

- 收盘上破最近摆动高点：原来是空头则记 CHoCH，否则记 BOS，偏向转为多头。
- 收盘下破最近摆动低点：原来是多头则记 CHoCH，否则记 BOS，偏向转为空头。
- 还没有突破时偏向是 ``neutral``。第一笔突破记为 BOS，不是 CHoCH。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from dataprice.strategy.candles import Candle

Bias = Literal["bull", "bear", "neutral"]
SwingKind = Literal["high", "low"]
EventKind = Literal["bos", "choch"]


@dataclass(frozen=True)
class Swing:
    index: int
    kind: SwingKind
    price: float
    confirmed_index: int


@dataclass(frozen=True)
class StructureEvent:
    index: int
    kind: EventKind
    bias: Literal["bull", "bear"]
    level: float


@dataclass(frozen=True)
class StructurePath:
    bias: tuple[str, ...]
    events: tuple[StructureEvent, ...]
    swings: tuple[Swing, ...]


def structure_path(candles: Sequence[Candle], wing: int = 2) -> StructurePath:
    if wing < 1:
        raise ValueError("wing 必须 >= 1")
    bias: list[str] = []
    events: list[StructureEvent] = []
    swings: list[Swing] = []
    last_high: Swing | None = None
    last_low: Swing | None = None
    high_broken = False
    low_broken = False
    current: Bias = "neutral"
    for i, candle in enumerate(candles):
        pivot = i - wing
        if pivot >= wing:
            if _is_swing_high(candles, pivot, wing):
                last_high = Swing(pivot, "high", candles[pivot].high, i)
                swings.append(last_high)
                high_broken = False
            if _is_swing_low(candles, pivot, wing):
                last_low = Swing(pivot, "low", candles[pivot].low, i)
                swings.append(last_low)
                low_broken = False
        broke_high = (
            last_high is not None and not high_broken and candle.close > last_high.price
        )
        broke_low = last_low is not None and not low_broken and candle.close < last_low.price
        if broke_high and broke_low and last_high is not None and last_low is not None:
            up = candle.close - last_high.price
            down = last_low.price - candle.close
            if current == "bull" or (current == "neutral" and down > up):
                broke_high = False
            else:
                broke_low = False
        if broke_high and last_high is not None:
            high_broken = True
            kind: EventKind = "choch" if current == "bear" else "bos"
            current = "bull"
            events.append(StructureEvent(i, kind, "bull", last_high.price))
        elif broke_low and last_low is not None:
            low_broken = True
            kind = "choch" if current == "bull" else "bos"
            current = "bear"
            events.append(StructureEvent(i, kind, "bear", last_low.price))
        bias.append(current)
    return StructurePath(bias=tuple(bias), events=tuple(events), swings=tuple(swings))


def _is_swing_high(candles: Sequence[Candle], pivot: int, wing: int) -> bool:
    price = candles[pivot].high
    for offset in range(1, wing + 1):
        if candles[pivot - offset].high >= price or candles[pivot + offset].high >= price:
            return False
    return True


def _is_swing_low(candles: Sequence[Candle], pivot: int, wing: int) -> bool:
    price = candles[pivot].low
    for offset in range(1, wing + 1):
        if candles[pivot - offset].low <= price or candles[pivot + offset].low <= price:
            return False
    return True
