"""4H 回踩订单块或公允价值缺口。摆动高低点不单独开仓。

做多：这根最低价落在支撑区里，但不破下沿，收盘回到区的上方。做空对称。
刺破远端再收回不算。止损在这根回踩影线之外。缓冲为 0 时，做多止损是最低价，做空是最高价。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from dataprice.contracts import Side
from dataprice.strategy.candles import Candle

ZoneKind = Literal["order_block", "fvg"]
_KIND_RANK = {"order_block": 0, "fvg": 1}


@dataclass(frozen=True)
class Zone:
    """``formed`` 是这区可以开始被回踩的那根下标。回踩必须更晚。"""

    kind: ZoneKind
    side: Side
    low: float
    high: float
    formed: int


@dataclass(frozen=True)
class Retest:
    side: Side
    kind: ZoneKind
    zone_low: float
    zone_high: float
    wick: float


def liquidity_zones(bars: Sequence[Candle]) -> tuple[Zone, ...]:
    """订单块和公允价值缺口。摆动点只给日线偏向用，不在这里。"""
    zones = _order_blocks(bars)
    zones.extend(_fvgs(bars))
    return tuple(zones)


def find_retest(
    bars: Sequence[Candle],
    index: int,
    zones: Sequence[Zone] | None = None,
) -> Retest | None:
    """这根第一次碰到某个还没被碰过的区，并且收盘站回有利的一侧。

    影线破了区的远端，不算回踩。同一根支撑和压力都成立，放弃。
    同一侧有好几处，取形成得最晚的；形成时刻相同，先订单块，再缺口。
    """
    if index < 1 or index >= len(bars):
        return None
    zones = liquidity_zones(bars) if zones is None else zones
    bar = bars[index]
    hits = [
        zone
        for zone in zones
        if zone.formed < index and _fresh(bars, zone, index) and _holds(bar, zone)
    ]
    buys = [zone for zone in hits if zone.side == "buy"]
    sells = [zone for zone in hits if zone.side == "sell"]
    if buys and sells:
        return None
    chosen_list = buys or sells
    if not chosen_list:
        return None
    chosen = max(chosen_list, key=lambda zone: (zone.formed, -_KIND_RANK[zone.kind]))
    return Retest(
        side=chosen.side,
        kind=chosen.kind,
        zone_low=chosen.low,
        zone_high=chosen.high,
        wick=bar.low if chosen.side == "buy" else bar.high,
    )


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


def _order_blocks(bars: Sequence[Candle]) -> list[Zone]:
    """阴线的下一根收盘站上它的最高价，是看涨订单块。阳线对称是看跌。十字星不算。"""
    zones: list[Zone] = []
    for index in range(len(bars) - 1):
        candle = bars[index]
        nxt = bars[index + 1]
        if candle.close < candle.open and nxt.close > candle.high:
            zones.append(Zone("order_block", "buy", candle.low, candle.high, index + 1))
        elif candle.close > candle.open and nxt.close < candle.low:
            zones.append(Zone("order_block", "sell", candle.low, candle.high, index + 1))
    return zones


def _fvgs(bars: Sequence[Candle]) -> list[Zone]:
    """第三根和第一根之间留白。看涨区从第一根最高价到第三根最低价。看跌反过来。"""
    zones: list[Zone] = []
    for index in range(len(bars) - 2):
        left = bars[index]
        right = bars[index + 2]
        if right.low > left.high:
            zones.append(Zone("fvg", "buy", left.high, right.low, index + 2))
        elif right.high < left.low:
            zones.append(Zone("fvg", "sell", right.high, left.low, index + 2))
    return zones


def _fresh(bars: Sequence[Candle], zone: Zone, index: int) -> bool:
    """形成之后到这根之前，还没有别的 K 线进过这个区。"""
    for cursor in range(zone.formed + 1, index):
        if _entered(bars[cursor], zone):
            return False
    return True


def _entered(bar: Candle, zone: Zone) -> bool:
    if zone.side == "buy":
        return bar.low <= zone.high
    return bar.high >= zone.low


def _holds(bar: Candle, zone: Zone) -> bool:
    """影线进区，但不破远端，收盘回到有利一侧。"""
    if zone.side == "buy":
        return zone.low <= bar.low <= zone.high and bar.close > zone.high
    return zone.low <= bar.high <= zone.high and bar.close < zone.low
