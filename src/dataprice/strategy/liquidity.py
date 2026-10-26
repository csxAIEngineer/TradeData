"""流动性：等高/等低、前高前低、时段高低，以及扫流动性。

时段按 UTC 切，互不重叠，用来标记上一段的高点和低点：

- asia：00:00–08:00
- london：08:00–13:00
- new_york：13:00–21:00
- 21:00–24:00 不记时段

前高前低是上一个 UTC 日的最高价和最低价。等高/等低是最近
``eq_lookback`` 根之内、价差不超过 ``eq_bps`` 个基点的两个摆动高点或低点。

一根流动性要到它形成之后的 K 线才能被扫。扫卖侧（低点）是最低价刺破、
收盘回到水位之上；扫买侧（高点）相反。扫到之后这档水位退出，极值是那根影线。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from dataprice.strategy.candles import Candle
from dataprice.strategy.structure import Swing
from dataprice.timeutil import UTC

LiquiditySide = Literal["buy", "sell"]

# (名字, 起始小时, 结束小时)，结束小时不含。
SESSIONS = (
    ("asia", 0, 8),
    ("london", 8, 13),
    ("new_york", 13, 21),
)


@dataclass(frozen=True)
class Level:
    kind: str
    side: LiquiditySide
    price: float
    known_index: int


@dataclass(frozen=True)
class Sweep:
    index: int
    side: LiquiditySide
    level: Level
    extreme: float


@dataclass(frozen=True)
class LiquidityMap:
    levels: tuple[Level, ...]
    sweeps: tuple[Sweep, ...]


@dataclass(frozen=True)
class LiquidityParams:
    eq_bps: float = 8.0
    eq_lookback: int = 48

    def __post_init__(self) -> None:
        if self.eq_bps <= 0:
            raise ValueError("eq_bps 必须大于 0")
        if self.eq_lookback < 1:
            raise ValueError("eq_lookback 必须 >= 1")


def session_name(ts: datetime) -> str | None:
    hour = ts.astimezone(UTC).hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return None


def map_liquidity(
    candles: Sequence[Candle],
    swings: Sequence[Swing],
    params: LiquidityParams | None = None,
) -> LiquidityMap:
    params = params or LiquidityParams()
    levels: list[Level] = []
    levels.extend(_equal_levels([s for s in swings if s.kind == "high"], "buy", "eqh", params))
    levels.extend(_equal_levels([s for s in swings if s.kind == "low"], "sell", "eql", params))
    levels.extend(_session_and_prior(candles))
    levels.sort(key=lambda level: (level.known_index, level.price, level.kind))
    return LiquidityMap(levels=tuple(levels), sweeps=find_sweeps(candles, levels))


def find_sweeps(candles: Sequence[Candle], levels: Sequence[Level]) -> tuple[Sweep, ...]:
    pending: dict[int, list[Level]] = {}
    for level in levels:
        pending.setdefault(level.known_index, []).append(level)
    active: list[Level] = []
    sweeps: list[Sweep] = []
    for index, candle in enumerate(candles):
        still: list[Level] = []
        for level in active:
            sweep = _sweep(index, candle, level)
            if sweep is None:
                still.append(level)
            else:
                sweeps.append(sweep)
        still.extend(pending.get(index, ()))
        active = still
    return tuple(sweeps)


def _equal_levels(
    swings: Sequence[Swing],
    side: LiquiditySide,
    kind: str,
    params: LiquidityParams,
) -> list[Level]:
    ordered = sorted(swings, key=lambda swing: swing.confirmed_index)
    levels: list[Level] = []
    for pos, swing in enumerate(ordered):
        for prev in reversed(ordered[:pos]):
            if swing.confirmed_index - prev.confirmed_index > params.eq_lookback:
                break
            if _within_bps(prev.price, swing.price, params.eq_bps):
                levels.append(
                    Level(
                        kind=kind,
                        side=side,
                        price=(prev.price + swing.price) / 2.0,
                        known_index=swing.confirmed_index,
                    )
                )
                break
    return levels


def _session_and_prior(candles: Sequence[Candle]) -> list[Level]:
    if not candles:
        return []
    levels: list[Level] = []
    first = candles[0].open_ts.astimezone(UTC)
    current_session = _session_key(first)
    session_high = candles[0].high
    session_low = candles[0].low
    current_day = first.date()
    day_high = candles[0].high
    day_low = candles[0].low
    for index in range(1, len(candles)):
        ts = candles[index].open_ts.astimezone(UTC)
        session = _session_key(ts)
        day = ts.date()
        if session != current_session:
            if current_session is not None:
                levels.append(Level("session_high", "buy", session_high, index))
                levels.append(Level("session_low", "sell", session_low, index))
            current_session = session
            session_high = candles[index].high
            session_low = candles[index].low
        elif current_session is not None:
            session_high = max(session_high, candles[index].high)
            session_low = min(session_low, candles[index].low)
        if day != current_day:
            levels.append(Level("pdh", "buy", day_high, index))
            levels.append(Level("pdl", "sell", day_low, index))
            current_day = day
            day_high = candles[index].high
            day_low = candles[index].low
        else:
            day_high = max(day_high, candles[index].high)
            day_low = min(day_low, candles[index].low)
    return levels


def _session_key(ts: datetime) -> tuple[object, str] | None:
    name = session_name(ts)
    if name is None:
        return None
    return (ts.date(), name)


def _sweep(index: int, candle: Candle, level: Level) -> Sweep | None:
    if level.side == "sell" and candle.low < level.price and candle.close > level.price:
        return Sweep(index=index, side="sell", level=level, extreme=candle.low)
    if level.side == "buy" and candle.high > level.price and candle.close < level.price:
        return Sweep(index=index, side="buy", level=level, extreme=candle.high)
    return None


def _within_bps(left: float, right: float, bps: float) -> bool:
    base = max(abs(left), abs(right), 1e-12)
    return abs(left - right) / base * 10_000 <= bps
