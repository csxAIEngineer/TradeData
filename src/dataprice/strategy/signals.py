"""ICT + SMC + 流动性入场。这一层不访问数据库。

规则（BTC，30 分钟入场，1 小时定偏向）：

1. 结构。1 小时用 BOS / CHoCH 定偏向。30 分钟也要走出同向结构。
   两边都不是该方向时不做。``neutral`` 不做。
2. 流动性。30 分钟上标记等高/等低、UTC 前高前低、上一段 asia / london /
   new_york 的高低点。只做「先扫掉逆向流动性」之后的单：做多要先扫卖侧
   （低点被影线刺破且收盘回到水位上），做空要先扫买侧。
3. 失衡。扫完之后 ``sweep_window`` 根内，要出现同向的 FVG 或订单块。
   订单块只挂在形成该 FVG 的那段位移上。
4. 回踩。缺口形成之后 ``return_window`` 根内，价格回到 FVG 或订单块，
   且收盘没有跌破（做多）或升破（做空）区间远端。同一根上 FVG 优先于订单块。
   成交价用这根收盘价。止损放在扫流动性那根的影线极值。
   止盈取至少 ``min_target_rr`` 以外、最近的反向流动性；没有就用 ``target_rr``。

信号在当根收盘才知道。``to_factor_frame`` 把偏向和信号收成因子面板，
索引是这根 K 线的收盘时间，可以直接交给 ``FactorStore.write_symbol_factors``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

import pandas as pd

from dataprice.contracts import Side
from dataprice.factors.frame import validate_panel
from dataprice.strategy.candles import Candle, assert_sorted, bar_delta
from dataprice.strategy.imbalance import Zone, find_zones
from dataprice.strategy.liquidity import Level, LiquidityMap, Sweep, map_liquidity
from dataprice.strategy.structure import structure_path

TradeBias = Literal["bull", "bear"]


@dataclass(frozen=True)
class StrategyParams:
    wing: int = 2
    eq_bps: float = 8.0
    eq_lookback: int = 48
    sweep_window: int = 8
    return_window: int = 12
    ob_lookback: int = 5
    target_rr: float = 2.0
    min_target_rr: float = 1.0

    def __post_init__(self) -> None:
        if self.wing < 1:
            raise ValueError("wing 必须 >= 1")
        if self.eq_bps <= 0 or self.eq_lookback < 1:
            raise ValueError("等高/等低参数不合法")
        if self.sweep_window < 1 or self.return_window < 1 or self.ob_lookback < 1:
            raise ValueError("窗口必须 >= 1")
        if self.target_rr <= 0 or self.min_target_rr <= 0:
            raise ValueError("盈亏比必须大于 0")


@dataclass(frozen=True)
class Signal:
    index: int
    open_ts: datetime
    side: Side
    entry: float
    stop: float
    target: float
    zone_kind: Literal["fvg", "ob"]
    sweep_index: int
    zone_index: int
    liquidity: str

    @property
    def reason(self) -> str:
        bias = "bull" if self.side == "buy" else "bear"
        return f"{bias}_{self.zone_kind}_after_{self.liquidity}"


@dataclass(frozen=True)
class SignalBook:
    candles: tuple[Candle, ...]
    signals: tuple[Signal, ...]
    bias_30m: tuple[str, ...]
    bias_1h: tuple[str, ...]


def generate_signals(
    candles_30: Sequence[Candle],
    candles_60: Sequence[Candle],
    params: StrategyParams | None = None,
) -> SignalBook:
    """30m 入场，60m（1 小时）定偏向。两列都要按开盘时间升序。"""
    params = params or StrategyParams()
    assert_sorted(candles_30)
    assert_sorted(candles_60)
    path_30 = structure_path(candles_30, params.wing)
    path_60 = structure_path(candles_60, params.wing)
    bias_1h = bias_asof(candles_60, path_60.bias, "60m", candles_30, "30m")
    liquidity = map_liquidity(candles_30, path_30.swings, _liquidity_params(params))
    zones = find_zones(candles_30, ob_lookback=params.ob_lookback)
    signals = entry_signals(
        candles_30,
        bias_30=path_30.bias,
        bias_1h=bias_1h,
        liquidity=liquidity,
        zones=zones,
        params=params,
    )
    return SignalBook(
        candles=tuple(candles_30),
        signals=tuple(signals),
        bias_30m=path_30.bias,
        bias_1h=bias_1h,
    )


def bias_asof(
    higher: Sequence[Candle],
    higher_bias: Sequence[str],
    higher_freq: str,
    lower: Sequence[Candle],
    lower_freq: str,
) -> tuple[str, ...]:
    """低周期收盘时，只用已经收盘的高周期偏向。"""
    if len(higher) != len(higher_bias):
        raise ValueError("高周期 bias 长度不匹配")
    closes = [candle.open_ts + bar_delta(higher_freq) for candle in higher]
    aligned: list[str] = []
    cursor = -1
    for candle in lower:
        known_at = candle.open_ts + bar_delta(lower_freq)
        while cursor + 1 < len(higher) and closes[cursor + 1] <= known_at:
            cursor += 1
        aligned.append(higher_bias[cursor] if cursor >= 0 else "neutral")
    return tuple(aligned)


def entry_signals(
    candles: Sequence[Candle],
    *,
    bias_30: Sequence[str],
    bias_1h: Sequence[str],
    liquidity: LiquidityMap,
    zones: Sequence[Zone],
    params: StrategyParams | None = None,
) -> list[Signal]:
    params = params or StrategyParams()
    if len(bias_30) != len(candles) or len(bias_1h) != len(candles):
        raise ValueError("bias 长度必须和 30m K 线一致")
    used_zones: set[int] = set()
    used_bars: set[int] = set()
    signals: list[Signal] = []
    for sweep in liquidity.sweeps:
        signal = _from_sweep(
            candles,
            sweep,
            bias_30=bias_30,
            bias_1h=bias_1h,
            levels=liquidity.levels,
            sweeps=liquidity.sweeps,
            zones=zones,
            used_zones=used_zones,
            params=params,
        )
        if signal is None or signal.index in used_bars:
            continue
        signals.append(signal)
        used_bars.add(signal.index)
    return signals


def to_factor_frame(book: SignalBook) -> pd.DataFrame:
    """收盘时刻才可知的因子面板。1 为多，-1 为空，0 为没有。"""
    if not book.candles:
        raise ValueError("没有 K 线，不能做因子面板")
    if len(book.bias_30m) != len(book.candles) or len(book.bias_1h) != len(book.candles):
        raise ValueError("bias 长度必须和 K 线一致")
    index = [candle.close_ts("30m") for candle in book.candles]
    signal = [0.0] * len(book.candles)
    for item in book.signals:
        signal[item.index] = 1.0 if item.side == "buy" else -1.0
    frame = pd.DataFrame(
        {
            "bias_1h": [_bias_code(value) for value in book.bias_1h],
            "bias_30m": [_bias_code(value) for value in book.bias_30m],
            "signal": signal,
        },
        index=pd.DatetimeIndex(index, name="exchange_ts"),
    )
    return validate_panel(frame)


def _from_sweep(
    candles: Sequence[Candle],
    sweep: Sweep,
    *,
    bias_30: Sequence[str],
    bias_1h: Sequence[str],
    levels: Sequence[Level],
    sweeps: Sequence[Sweep],
    zones: Sequence[Zone],
    used_zones: set[int],
    params: StrategyParams,
) -> Signal | None:
    trade_bias: TradeBias = "bull" if sweep.side == "sell" else "bear"
    candidates = [
        zone
        for zone in zones
        if id(zone) not in used_zones
        and zone.bias == trade_bias
        and sweep.index < zone.index <= sweep.index + params.sweep_window
        and _aligned(bias_30, bias_1h, zone.index, trade_bias)
    ]
    if not candidates:
        return None
    alive = {id(zone): zone for zone in candidates}
    start = min(zone.index for zone in candidates) + 1
    end = min(
        len(candles),
        max(zone.index + params.return_window for zone in candidates) + 1,
    )
    for index in range(start, end):
        ready: list[Zone] = []
        for key, zone in list(alive.items()):
            if index <= zone.index:
                continue
            if index > zone.index + params.return_window:
                alive.pop(key, None)
                continue
            candle = candles[index]
            if _invalid(candle, zone):
                alive.pop(key, None)
                continue
            if _touches(candle, zone) and _holds(candle, zone):
                ready.append(zone)
        if not ready or not _aligned(bias_30, bias_1h, index, trade_bias):
            continue
        ready.sort(key=lambda zone: (0 if zone.kind == "fvg" else 1, zone.index))
        zone = ready[0]
        signal = _signal(candles, index, sweep, zone, levels, sweeps, params)
        if signal is None:
            continue
        used_zones.add(id(zone))
        return signal
    return None


def _signal(
    candles: Sequence[Candle],
    index: int,
    sweep: Sweep,
    zone: Zone,
    levels: Sequence[Level],
    sweeps: Sequence[Sweep],
    params: StrategyParams,
) -> Signal | None:
    candle = candles[index]
    entry = candle.close
    stop = sweep.extreme
    risk = entry - stop if zone.bias == "bull" else stop - entry
    if risk <= 0:
        return None
    target = _target(
        entry,
        risk,
        zone.bias,
        index,
        levels,
        sweeps,
        params,
    )
    if zone.bias == "bull" and target <= entry:
        return None
    if zone.bias == "bear" and target >= entry:
        return None
    return Signal(
        index=index,
        open_ts=candle.open_ts,
        side="buy" if zone.bias == "bull" else "sell",
        entry=entry,
        stop=stop,
        target=target,
        zone_kind=zone.kind,
        sweep_index=sweep.index,
        zone_index=zone.index,
        liquidity=sweep.level.kind,
    )


def _target(
    entry: float,
    risk: float,
    bias: TradeBias,
    index: int,
    levels: Sequence[Level],
    sweeps: Sequence[Sweep],
    params: StrategyParams,
) -> float:
    spent = {id(sweep.level) for sweep in sweeps if sweep.index <= index}
    floor = entry + risk * params.min_target_rr if bias == "bull" else entry - risk * params.min_target_rr
    best: float | None = None
    for level in levels:
        if level.known_index >= index or id(level) in spent:
            continue
        if bias == "bull" and level.side == "buy" and level.price >= floor:
            if best is None or level.price < best:
                best = level.price
        if bias == "bear" and level.side == "sell" and level.price <= floor:
            if best is None or level.price > best:
                best = level.price
    if best is not None:
        return best
    if bias == "bull":
        return entry + risk * params.target_rr
    return entry - risk * params.target_rr


def _aligned(bias_30: Sequence[str], bias_1h: Sequence[str], index: int, bias: str) -> bool:
    return bias_30[index] == bias and bias_1h[index] == bias


def _touches(candle: Candle, zone: Zone) -> bool:
    return candle.low <= zone.top and candle.high >= zone.bottom


def _holds(candle: Candle, zone: Zone) -> bool:
    if zone.bias == "bull":
        return candle.close >= zone.bottom
    return candle.close <= zone.top


def _invalid(candle: Candle, zone: Zone) -> bool:
    if zone.bias == "bull":
        return candle.close < zone.bottom
    return candle.close > zone.top


def _bias_code(value: str) -> float:
    if value == "bull":
        return 1.0
    if value == "bear":
        return -1.0
    return 0.0


def _liquidity_params(params: StrategyParams):
    from dataprice.strategy.liquidity import LiquidityParams

    return LiquidityParams(eq_bps=params.eq_bps, eq_lookback=params.eq_lookback)
