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

可选过滤（默认不收紧，回测评估时再打开）：只做某些失衡和流动性、
限制止损宽度和追价、要求缺口够大、用 4 小时结构确认 1 小时偏向。
``exit_on_bias_flip`` 为假时，1 小时偏向翻转不作为出场，只留止损和止盈。
``breakeven_at_r`` 在价格先走出这么多 R 之后，下一根把止损抬到进场价。

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
from dataprice.strategy.candles import Candle, aggregate_candles, assert_sorted, bar_delta
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
    zone_kinds: tuple[str, ...] = ("fvg", "ob")
    liquidity_kinds: tuple[str, ...] = (
        "eqh",
        "eql",
        "pdh",
        "pdl",
        "session_high",
        "session_low",
    )
    sides: tuple[str, ...] = ("buy", "sell")
    max_risk_bps: float | None = None
    max_chase_bps: float | None = None
    min_gap_bps: float = 0.0
    confirm_4h: bool = False
    exit_on_bias_flip: bool = True
    breakeven_at_r: float | None = None
    entry: Literal["close", "zone"] = "close"
    max_structure_age: int | None = None
    structure_kinds: tuple[str, ...] = ("bos", "choch")

    def __post_init__(self) -> None:
        if self.wing < 1:
            raise ValueError("wing 必须 >= 1")
        if self.eq_bps <= 0 or self.eq_lookback < 1:
            raise ValueError("等高/等低参数不合法")
        if self.sweep_window < 1 or self.return_window < 1 or self.ob_lookback < 1:
            raise ValueError("窗口必须 >= 1")
        if self.target_rr <= 0 or self.min_target_rr <= 0:
            raise ValueError("盈亏比必须大于 0")
        if not self.zone_kinds or not self.liquidity_kinds or not self.sides:
            raise ValueError("失衡、流动性或方向不能是空的")
        if any(side not in ("buy", "sell") for side in self.sides):
            raise ValueError("方向只能是 buy 或 sell")
        if self.max_risk_bps is not None and self.max_risk_bps <= 0:
            raise ValueError("max_risk_bps 必须大于 0")
        if self.max_chase_bps is not None and self.max_chase_bps < 0:
            raise ValueError("max_chase_bps 不能为负")
        if self.min_gap_bps < 0:
            raise ValueError("min_gap_bps 不能为负")
        if self.breakeven_at_r is not None and self.breakeven_at_r <= 0:
            raise ValueError("breakeven_at_r 必须大于 0")
        if self.entry not in ("close", "zone"):
            raise ValueError("entry 只能是 close 或 zone")
        if self.max_structure_age is not None and self.max_structure_age < 0:
            raise ValueError("max_structure_age 不能为负")
        if not self.structure_kinds:
            raise ValueError("structure_kinds 不能是空的")


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


def evaluated_params() -> StrategyParams:
    """训练窗口里最好的候选，不是已经证实的优势。

    4 小时结构同向，只做等高/等低，止损不超过 80 个基点，
    限价进在失衡近端，1 小时偏向翻转不再出场。
    2026-03 之后的留出样本在扣费后是亏的。见 ``doc/ict_smc_evaluation.md``。
    """
    return StrategyParams(
        confirm_4h=True,
        max_risk_bps=80,
        liquidity_kinds=("eqh", "eql"),
        exit_on_bias_flip=False,
        entry="zone",
    )


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
    structure_age = higher_event_age(candles_60, path_60.events, "60m", candles_30, "30m")
    bias_4h: tuple[str, ...] | None = None
    if params.confirm_4h:
        candles_4h = aggregate_candles(candles_60, "4h")
        path_4h = structure_path(candles_4h, params.wing)
        bias_4h = bias_asof(candles_4h, path_4h.bias, "4h", candles_30, "30m")
    liquidity = map_liquidity(candles_30, path_30.swings, _liquidity_params(params))
    zones = find_zones(candles_30, ob_lookback=params.ob_lookback)
    signals = entry_signals(
        candles_30,
        bias_30=path_30.bias,
        bias_1h=bias_1h,
        liquidity=liquidity,
        zones=zones,
        params=params,
        bias_4h=bias_4h,
        structure_age=structure_age,
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
    bias_4h: Sequence[str] | None = None,
    structure_age: Sequence[tuple[str, str, int] | None] | None = None,
) -> list[Signal]:
    params = params or StrategyParams()
    if len(bias_30) != len(candles) or len(bias_1h) != len(candles):
        raise ValueError("bias 长度必须和 30m K 线一致")
    if bias_4h is not None and len(bias_4h) != len(candles):
        raise ValueError("4h bias 长度必须和 30m K 线一致")
    if structure_age is not None and len(structure_age) != len(candles):
        raise ValueError("结构年龄长度必须和 30m K 线一致")
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
            bias_4h=bias_4h,
            structure_age=structure_age,
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
    bias_4h: Sequence[str] | None = None,
    structure_age: Sequence[tuple[str, str, int] | None] | None = None,
) -> Signal | None:
    trade_bias: TradeBias = "bull" if sweep.side == "sell" else "bear"
    if (trade_bias == "bull" and "buy" not in params.sides) or (
        trade_bias == "bear" and "sell" not in params.sides
    ):
        return None
    if sweep.level.kind not in params.liquidity_kinds:
        return None
    candidates = [
        zone
        for zone in zones
        if id(zone) not in used_zones
        and zone.kind in params.zone_kinds
        and zone.bias == trade_bias
        and sweep.index < zone.index <= sweep.index + params.sweep_window
        and _aligned(bias_30, bias_1h, zone.index, trade_bias, bias_4h)
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
        if not ready or not _aligned(bias_30, bias_1h, index, trade_bias, bias_4h):
            continue
        if not _structure_fresh(structure_age, index, trade_bias, params):
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
    entry = _entry_price(candle, zone, params.entry)
    if entry is None:
        return None
    stop = sweep.extreme
    risk = entry - stop if zone.bias == "bull" else stop - entry
    if risk <= 0:
        return None
    if params.max_risk_bps is not None and risk / entry * 10_000 > params.max_risk_bps:
        return None
    if params.max_chase_bps is not None and _chase_bps(entry, zone) > params.max_chase_bps:
        return None
    if zone.kind == "fvg" and params.min_gap_bps > 0:
        if (zone.top - zone.bottom) / entry * 10_000 < params.min_gap_bps:
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


def _aligned(
    bias_30: Sequence[str],
    bias_1h: Sequence[str],
    index: int,
    bias: str,
    bias_4h: Sequence[str] | None = None,
) -> bool:
    if bias_30[index] != bias or bias_1h[index] != bias:
        return False
    return bias_4h is None or bias_4h[index] == bias


def higher_event_age(
    higher: Sequence[Candle],
    events: Sequence[object],
    higher_freq: str,
    lower: Sequence[Candle],
    lower_freq: str,
) -> tuple[tuple[str, str, int] | None, ...]:
    """每根低周期 K 线对应最近一个已经收盘的高周期结构事件，以及过了多少根。"""
    known = sorted(
        (
            higher[event.index].open_ts + bar_delta(higher_freq),
            event.bias,
            event.kind,
        )
        for event in events
    )
    out: list[tuple[str, str, int] | None] = []
    cursor = -1
    known_at = -1
    for index, candle in enumerate(lower):
        stamp = candle.open_ts + bar_delta(lower_freq)
        while cursor + 1 < len(known) and known[cursor + 1][0] <= stamp:
            cursor += 1
            known_at = index
        if cursor < 0:
            out.append(None)
        else:
            _, bias, kind = known[cursor]
            out.append((bias, kind, index - known_at))
    return tuple(out)


def _structure_fresh(
    structure_age: Sequence[tuple[str, str, int] | None] | None,
    index: int,
    bias: str,
    params: StrategyParams,
) -> bool:
    if params.max_structure_age is None and set(params.structure_kinds) == {"bos", "choch"}:
        return True
    if structure_age is None:
        return params.max_structure_age is None
    state = structure_age[index]
    if state is None:
        return False
    event_bias, kind, age = state
    if event_bias != bias or kind not in params.structure_kinds:
        return False
    return params.max_structure_age is None or age <= params.max_structure_age


def _entry_price(candle: Candle, zone: Zone, mode: str) -> float | None:
    if mode == "close":
        return candle.close
    if zone.bias == "bull":
        if candle.open > zone.top:
            return zone.top if candle.low <= zone.top else None
        if candle.open < zone.bottom:
            return None
        return candle.open
    if candle.open < zone.bottom:
        return zone.bottom if candle.high >= zone.bottom else None
    if candle.open > zone.top:
        return None
    return candle.open


def _chase_bps(close: float, zone: Zone) -> float:
    """收盘离开区间近端的距离。还在区间里就是 0。"""
    if close <= 0:
        return 0.0
    if zone.bias == "bull" and close > zone.top:
        return (close - zone.top) / close * 10_000
    if zone.bias == "bear" and close < zone.bottom:
        return (zone.bottom - close) / close * 10_000
    return 0.0


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
