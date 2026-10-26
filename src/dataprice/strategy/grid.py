"""猴市震荡网格。这一层不读数据库，也不改趋势候选的默认参数。

趋势侧 ``regime_params`` 要求大约三天（18 根已收盘的 4 小时）的 Kaufman
效率比至少 0.25 才做单边。这里用同一条线的另一侧：效率比低于 0.25 才挂网格，
等于 0.25 或更高就空仓。样本不够也空仓。网格不产生 ICT 入场。

区间优先用已经确认的等高（eqh）和等低（eql）：上一根收盘价上方最近的
eqh 当上沿，下方最近的 eql 当下沿。没有成对水位，或间距放不进这对水位时，
退回最近 ``range_lookback`` 根 30 分钟的最高价和最低价（区间上下沿）。
计算某一根时只用它之前已经知道的水位和 K 线。

网格间距是中轴价格的固定基点。半边里每隔一个间距挂一档，最外一档离区间
边缘至少一个间距，避免把止损挂在进场价上。多头只挂在中轴下方，空头只挂
在上方。一手一个档位，同一边的手数不超过 ``max_inventory_units``。
只有这根 K 线的最高价和最低价包住档位才成交。跳空没有印到的价格不成交。

出场有三档，同一根先看坏的：

- 收盘跌破下沿（或多头仓的上沿对称位置）之外 ``range_break_bps``，
  整仓止损。影线刺破但收盘回到区间内不算破位，那是扫流动性。
- 已经持有的仓位，价格碰到往中轴方向的下一档，按那一档止盈。
  进场和止盈落在同一根上时这笔不做，避免一根大 K 线来回各算一次。
- 当前库存的盯市浮亏达到这份名义的 ``max_drawdown_bps`` 就整仓平掉，
  之后 ``cooldown_bars`` 根不再开仓。额度按当时的手数算，不把已经实现的
  历史亏损当成下一笔必须立刻再砍一次的理由。默认 600 个基点，宽过常见的
  一天波动，平常先由收盘破位出场。

效率比重新站上阈值时，下一根开盘价平掉库存，不把网格留进单边。

成交假设和 ``backtest`` 的标题成本相同：每边手续费 ``fee_bps``，
每边成交往不利方向滑 ``slippage_bps``。默认 5 和 2，往返 14 个基点。
没有资金费，也没有部分成交。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence

from dataprice.config import Settings
from dataprice.contracts import Side, q_cash, q_px, q_qty
from dataprice.strategy.candles import Candle, aggregate_candles, assert_sorted
from dataprice.strategy.liquidity import Level, LiquidityParams, map_liquidity
from dataprice.strategy.regime import efficiency_asof
from dataprice.strategy.structure import structure_path

# 与 doc/ict_smc_evaluation.md 相同：进场早于这一刻算训练，当天及之后算留出。
HOLDOUT_START = datetime(2026, 3, 1, tzinfo=timezone.utc)

GridExit = Literal["target", "range_break", "drawdown", "trend", "end"]
RangeKind = Literal["eq", "range"]


@dataclass(frozen=True)
class GridParams:
    """网格自己的参数。不从 ``StrategyParams`` 派生，避免碰到趋势默认值。"""

    max_er: float = 0.25
    er_bars: int = 18
    spacing_bps: float = 50.0
    max_inventory_units: int = 3
    unit_qty: Decimal = Decimal("0.01")
    range_lookback: int = 48
    eq_bps: float = 8.0
    eq_lookback: int = 48
    wing: int = 2
    range_break_bps: float = 15.0
    max_drawdown_bps: float = 600.0
    cooldown_bars: int = 48
    fee_bps: float = 5.0
    slippage_bps: float = 2.0

    def __post_init__(self) -> None:
        if not 0 < self.max_er <= 1:
            raise ValueError("max_er 必须在 0 和 1 之间")
        if self.er_bars < 2:
            raise ValueError("er_bars 必须 >= 2")
        if self.spacing_bps <= 0:
            raise ValueError("spacing_bps 必须大于 0")
        if self.max_inventory_units < 1:
            raise ValueError("max_inventory_units 必须 >= 1")
        if self.unit_qty <= 0:
            raise ValueError("unit_qty 必须大于 0")
        if self.range_lookback < 2:
            raise ValueError("range_lookback 必须 >= 2")
        if self.eq_bps <= 0 or self.eq_lookback < 1:
            raise ValueError("等高/等低参数不合法")
        if self.wing < 1:
            raise ValueError("wing 必须 >= 1")
        if self.range_break_bps < 0:
            raise ValueError("range_break_bps 不能为负")
        if self.max_drawdown_bps <= 0:
            raise ValueError("max_drawdown_bps 必须大于 0")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars 不能为负")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("手续费和滑点不能为负")


@dataclass(frozen=True)
class GridRung:
    side: Side
    k: int
    price: float
    target: float


@dataclass(frozen=True)
class Bracket:
    lower: float
    upper: float
    mid: float
    spacing: float
    kind: RangeKind
    rungs: tuple[GridRung, ...]
    stop_long: float
    stop_short: float


@dataclass(frozen=True)
class GridTrade:
    side: Side
    entry_index: int
    exit_index: int
    entry_ts: datetime
    exit_ts: datetime
    entry: Decimal
    exit: Decimal
    pnl: Decimal
    reason: GridExit
    range_kind: str
    lower: float
    upper: float


@dataclass(frozen=True)
class GridMark:
    index: int
    ts: datetime
    units: int
    peak_units: int
    equity: Decimal
    notional: Decimal


@dataclass(frozen=True)
class GridResult:
    trades: tuple[GridTrade, ...]
    path: tuple[GridMark, ...]
    chop_bars: int
    range_bars: int
    opens: int
    buying_power: Decimal

    @property
    def realized_pnl(self) -> Decimal:
        total = Decimal("0")
        for trade in self.trades:
            total += trade.pnl
        return q_cash(total)


@dataclass(frozen=True)
class GridStats:
    name: str
    trades: int
    pnl: Decimal
    win_rate: float
    profit_factor: float
    max_drawdown: Decimal
    realized_drawdown: Decimal
    max_inventory_units: int
    max_inventory_notional: Decimal
    end_inventory_units: int
    avg_hold_bars: float
    eq_trades: int
    range_trades: int
    buys: int
    buy_pnl: Decimal
    sells: int
    sell_pnl: Decimal


def active_bracket(
    candles: Sequence[Candle],
    levels: Sequence[Level],
    index: int,
    params: GridParams,
) -> Bracket | None:
    """这一根开盘前能看到的网格。不够窗口、收盘已经在区间外，就没有网格。"""
    if index < params.range_lookback:
        return None
    window = candles[index - params.range_lookback : index]
    roll_low = min(candle.low for candle in window)
    roll_high = max(candle.high for candle in window)
    prev = candles[index - 1].close
    if not prev > 0:
        return None
    lower_eq, upper_eq = _nearest_equal(levels, index, params.range_lookback, prev)
    if (
        lower_eq is not None
        and upper_eq is not None
        and lower_eq < prev < upper_eq
    ):
        bracket = _geometry(lower_eq, upper_eq, "eq", params)
        if bracket is not None:
            return bracket
    if roll_low < prev < roll_high:
        return _geometry(roll_low, roll_high, "range", params)
    return None


def run_grid(
    candles_30: Sequence[Candle],
    candles_60: Sequence[Candle],
    params: GridParams | None = None,
    *,
    buying_power: Decimal = Decimal("1000000"),
) -> GridResult:
    """30 分钟成交，60 分钟只用来合成已经收盘的 4 小时效率比。"""
    params = params or GridParams()
    assert_sorted(candles_30)
    assert_sorted(candles_60)
    candles_4h = aggregate_candles(candles_60, "4h")
    er_at = efficiency_asof(candles_4h, "4h", candles_30, "30m", params.er_bars)
    return simulate_grid(candles_30, er_at, params, buying_power=buying_power)


def simulate_grid(
    candles: Sequence[Candle],
    er_at: Sequence[float | None],
    params: GridParams | None = None,
    *,
    buying_power: Decimal = Decimal("1000000"),
) -> GridResult:
    """``er_at[i]`` 是第 i 根收盘时才知道的效率比。挂单用上一根的值。"""
    params = params or GridParams()
    assert_sorted(candles)
    if len(er_at) != len(candles):
        raise ValueError("效率比长度必须和 30m K 线一致")
    qty = q_qty(params.unit_qty)
    swings = structure_path(candles, params.wing).swings
    levels = map_liquidity(
        candles,
        swings,
        LiquidityParams(eq_bps=params.eq_bps, eq_lookback=params.eq_lookback),
    ).levels
    book = _Book(params, qty, buying_power)
    path: list[GridMark] = []
    frozen: Bracket | None = None
    halt_until = -1
    chop_bars = 0
    range_bars = 0
    last = len(candles) - 1
    for index, candle in enumerate(candles):
        bar_units = abs(_units(book.lots))
        if index > 0:
            previous = er_at[index - 1]
            enabled = previous is not None and previous < params.max_er
            if not enabled or index < halt_until:
                if book.lots:
                    book.flatten(
                        index,
                        candle,
                        candle.open,
                        "trend" if not enabled else "drawdown",
                    )
                    frozen = None
                    bar_units = max(bar_units, abs(_units(book.lots)))
            else:
                chop_bars += 1
                if not book.lots:
                    frozen = active_bracket(candles, levels, index, params)
                if frozen is not None:
                    range_bars += 1
                    stopped = _stopped(candle, frozen, book.lots)
                    if stopped is not None:
                        book.flatten(index, candle, stopped[0], stopped[1])
                        frozen = None
                    elif book.lots and book.take_profits(index, candle):
                        if not book.lots:
                            frozen = None
                    elif index < last:
                        book.enter(index, candle, frozen)
                        if not book.lots:
                            frozen = None
                    bar_units = max(bar_units, abs(_units(book.lots)))
            equity = book.equity(candle.close)
            if book.lots and frozen is not None:
                unrealized = q_cash(equity - book.realized)
                limit = _drawdown_limit(params, frozen.mid, abs(_units(book.lots)))
                if limit > 0 and -unrealized >= limit:
                    book.flatten(index, candle, candle.close, "drawdown")
                    frozen = None
                    halt_until = index + 1 + params.cooldown_bars
                    equity = book.equity(candle.close)
                    bar_units = max(bar_units, abs(_units(book.lots)))
            if index == last and book.lots:
                book.flatten(index, candle, candle.close, "end")
                frozen = None
                equity = book.equity(candle.close)
        else:
            equity = Decimal("0")
        path.append(
            GridMark(
                index=index,
                ts=candle.open_ts,
                units=_units(book.lots),
                peak_units=bar_units,
                equity=q_cash(equity),
                notional=q_cash(qty * Decimal(bar_units) * _px(candle.close)),
            )
        )
    return GridResult(
        trades=tuple(book.trades),
        path=tuple(path),
        chop_bars=chop_bars,
        range_bars=range_bars,
        opens=book.opens,
        buying_power=buying_power,
    )


def grid_windows(
    result: GridResult,
    holdout_start: datetime = HOLDOUT_START,
) -> tuple[GridStats, GridStats, GridStats]:
    """训练、留出、全窗口。成交按开仓时间切开；回撤和库存按 K 线时间重算。"""
    return (
        _stats(result, "训练", None, holdout_start),
        _stats(result, "留出", holdout_start, None),
        _stats(result, "全窗口", None, None),
    )


def format_grid_report(result: GridResult, params: GridParams | None = None) -> str:
    params = params or GridParams()
    round_trip = (params.fee_bps + params.slippage_bps) * 2
    lines = [
        "震荡网格：4 小时 Kaufman 效率比低于 "
        f"{params.max_er:g}（{params.er_bars} 根已收盘 4 小时）才开仓，"
        "不低于这个阈值就平仓。趋势候选的默认参数不动。",
        "成本：每边手续费 "
        f"{params.fee_bps:g} bps，不利滑点 {params.slippage_bps:g} bps，"
        f"往返 {round_trip:g} bps。没有资金费，没有部分成交。",
        "规则：间距 "
        f"{params.spacing_bps:g} bps，单侧最多 {params.max_inventory_units} 手，"
        f"每手 {params.unit_qty}。区间用 eqh/eql，否则用最近 "
        f"{params.range_lookback} 根上下沿。收盘破位 "
        f"{params.range_break_bps:g} bps 止损；当前库存浮亏 "
        f"{params.max_drawdown_bps:g} bps 停机 "
        f"{params.cooldown_bars} 根。",
        f"猴市 K 线 {result.chop_bars}，其中有区间 {result.range_bars}，开仓 {result.opens}。",
        "出场 "
        + "，".join(
            f"{name} {count}"
            for name, count in sorted(_reason_counts(result).items(), key=lambda item: item[0])
        ),
    ]
    for stats in grid_windows(result):
        factor = "inf" if stats.profit_factor == float("inf") else f"{stats.profit_factor:.2f}"
        lines.append(
            f"{stats.name} 成交 {stats.trades}，盈亏 {_signed(stats.pnl)}，"
            f"胜率 {stats.win_rate:.1%}，盈亏比 {factor}，"
            f"盯市回撤 {stats.max_drawdown:.4f}，已实现回撤 {stats.realized_drawdown:.4f}，"
            f"最大库存 {stats.max_inventory_units} 手 / {stats.max_inventory_notional:.4f}，"
            f"窗口结束库存 {stats.end_inventory_units}，"
            f"平均持有 {stats.avg_hold_bars:.1f} 根，"
            f"多 {stats.buys} ({_signed(stats.buy_pnl)})，"
            f"空 {stats.sells} ({_signed(stats.sell_pnl)})，"
            f"水位 {stats.eq_trades} / 区间 {stats.range_trades}"
        )
    return "\n".join(lines)


def run_stored_grid(
    *,
    symbol: str = "BTC-USDT",
    start: datetime,
    end: datetime,
    params: GridParams | None = None,
    buying_power: Decimal = Decimal("1000000"),
    settings: Settings | None = None,
) -> tuple[GridResult, int, int]:
    """从 ClickHouse ``market.bars`` 读 30m / 60m，再跑网格。"""
    from dataprice.market.repo import market_session
    from dataprice.strategy.candles import candles_from_frame

    params = params or GridParams()
    with market_session(settings) as repo:
        frame_30 = repo.query_bars([symbol], "30m", start, end)
        frame_60 = repo.query_bars([symbol], "60m", start, end)
    candles_30 = candles_from_frame(frame_30)
    candles_60 = candles_from_frame(frame_60)
    if not candles_30 or not candles_60:
        raise RuntimeError(
            "ClickHouse 里没有足够的 K 线。先执行: "
            f"uv run dataprice backfill --symbol {symbol}"
        )
    result = run_grid(candles_30, candles_60, params, buying_power=buying_power)
    return result, len(candles_30), len(candles_60)


def _nearest_equal(
    levels: Sequence[Level],
    index: int,
    lookback: int,
    prev: float,
) -> tuple[float | None, float | None]:
    oldest = index - lookback
    lower: float | None = None
    upper: float | None = None
    for level in levels:
        if level.known_index < oldest or level.known_index >= index:
            continue
        if level.kind == "eql" and level.price < prev and (lower is None or level.price > lower):
            lower = level.price
        elif level.kind == "eqh" and level.price > prev and (upper is None or level.price < upper):
            upper = level.price
    return lower, upper


def _geometry(
    lower: float,
    upper: float,
    kind: RangeKind,
    params: GridParams,
) -> Bracket | None:
    if upper <= lower:
        return None
    mid = (lower + upper) / 2.0
    if mid <= 0:
        return None
    spacing = mid * params.spacing_bps / 10_000.0
    if spacing <= 0:
        return None
    pad = params.range_break_bps / 10_000.0
    rungs: list[GridRung] = []
    k = 1
    while k <= 500:
        buy = mid - spacing * k
        sell = mid + spacing * k
        if buy < lower + spacing and sell > upper - spacing:
            break
        if buy >= lower + spacing:
            rungs.append(GridRung("buy", k, buy, mid - spacing * (k - 1)))
        if sell <= upper - spacing:
            rungs.append(GridRung("sell", k, sell, mid + spacing * (k - 1)))
        k += 1
    if not rungs:
        return None
    return Bracket(
        lower=lower,
        upper=upper,
        mid=mid,
        spacing=spacing,
        kind=kind,
        rungs=tuple(rungs),
        stop_long=lower * (1.0 - pad),
        stop_short=upper * (1.0 + pad),
    )


def _stopped(
    candle: Candle,
    bracket: Bracket,
    lots: Sequence[_Lot],
) -> tuple[float, GridExit] | None:
    """收盘留在破位价之外才止损。没有仓位时，这种 K 线只是不能开新仓。"""
    if not lots:
        if candle.close <= bracket.stop_long or candle.close >= bracket.stop_short:
            return (candle.close, "range_break")
        return None
    side = lots[0].rung.side
    if side == "buy":
        if candle.close > bracket.stop_long:
            return None
        price = candle.open if candle.open <= bracket.stop_long else candle.close
        return (price, "range_break")
    if candle.close < bracket.stop_short:
        return None
    price = candle.open if candle.open >= bracket.stop_short else candle.close
    return (price, "range_break")


def _crossed(candle: Candle, side: Side, price: float) -> bool:
    """档位要落在这根的最高价和最低价之间。跳空没印出来的价格不成交。"""
    del side
    return candle.low <= price <= candle.high


def _target_hit(candle: Candle, side: Side, target: float) -> bool:
    if side == "buy":
        return candle.high >= target
    return candle.low <= target


def _units(lots: Sequence[_Lot]) -> int:
    total = 0
    for lot in lots:
        total += 1 if lot.rung.side == "buy" else -1
    return total


def _drawdown_limit(params: GridParams, ref_price: float, units: int) -> Decimal:
    """当前库存名义上的浮亏额度。手数至少按 1 算，避免空仓算出 0。"""
    held = max(units, 1)
    notional = q_cash(params.unit_qty * Decimal(held) * _px(ref_price))
    return q_cash(notional * Decimal(str(params.max_drawdown_bps)) / Decimal("10000"))


def _stats(
    result: GridResult,
    name: str,
    start: datetime | None,
    end: datetime | None,
) -> GridStats:
    chosen = [
        trade
        for trade in result.trades
        if (start is None or trade.entry_ts >= start) and (end is None or trade.entry_ts < end)
    ]
    wins = [trade.pnl for trade in chosen if trade.pnl > 0]
    losses = [trade.pnl for trade in chosen if trade.pnl < 0]
    gross_win = sum(wins, Decimal("0"))
    gross_loss = sum(losses, Decimal("0"))
    if gross_loss < 0:
        factor = float(gross_win / -gross_loss)
    elif gross_win > 0:
        factor = float("inf")
    else:
        factor = 0.0
    equity = Decimal("0")
    peak = Decimal("0")
    realized_dd = Decimal("0")
    holds = 0
    for trade in chosen:
        equity += trade.pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > realized_dd:
            realized_dd = drawdown
        holds += trade.exit_index - trade.entry_index
    marks = [
        mark
        for mark in result.path
        if (start is None or mark.ts >= start) and (end is None or mark.ts < end)
    ]
    prior = Decimal("0")
    if start is not None:
        for mark in result.path:
            if mark.ts >= start:
                break
            prior = mark.equity
    mtm_peak = prior
    mtm_dd = Decimal("0")
    max_units = 0
    max_notional = Decimal("0")
    for mark in marks:
        if mark.equity > mtm_peak:
            mtm_peak = mark.equity
        drawdown = mtm_peak - mark.equity
        if drawdown > mtm_dd:
            mtm_dd = drawdown
        if mark.peak_units > max_units:
            max_units = mark.peak_units
        if mark.notional > max_notional:
            max_notional = mark.notional
    buys = [trade for trade in chosen if trade.side == "buy"]
    sells = [trade for trade in chosen if trade.side == "sell"]
    count = len(chosen)
    return GridStats(
        name=name,
        trades=count,
        pnl=q_cash(sum((trade.pnl for trade in chosen), Decimal("0"))),
        win_rate=(len(wins) / count) if count else 0.0,
        profit_factor=factor,
        max_drawdown=q_cash(mtm_dd),
        realized_drawdown=q_cash(realized_dd),
        max_inventory_units=max_units,
        max_inventory_notional=q_cash(max_notional),
        end_inventory_units=marks[-1].units if marks else 0,
        avg_hold_bars=(holds / count) if count else 0.0,
        eq_trades=sum(1 for trade in chosen if trade.range_kind == "eq"),
        range_trades=sum(1 for trade in chosen if trade.range_kind == "range"),
        buys=len(buys),
        buy_pnl=q_cash(sum((trade.pnl for trade in buys), Decimal("0"))),
        sells=len(sells),
        sell_pnl=q_cash(sum((trade.pnl for trade in sells), Decimal("0"))),
    )


def _reason_counts(result: GridResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in result.trades:
        counts[trade.reason] = counts.get(trade.reason, 0) + 1
    return counts


def _signed(value: Decimal) -> str:
    text = f"{value:.4f}"
    if value > 0:
        return "+" + text
    return text


def _slipped(price: float, side: Side, slippage_bps: float) -> float:
    if slippage_bps <= 0:
        return price
    slip = slippage_bps / 10_000.0
    if side == "buy":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def _px(value: float) -> Decimal:
    return q_px(Decimal(format(value, ".10f")))


def _fee(qty: Decimal, price: Decimal, fee_bps: float) -> Decimal:
    if fee_bps <= 0:
        return Decimal("0")
    raw = qty * price * Decimal(str(fee_bps)) / Decimal("10000")
    return q_cash(raw)


def _fill(price: float, book_side: Side, is_entry: bool, slippage_bps: float) -> Decimal:
    if is_entry:
        side = book_side
    else:
        side = "sell" if book_side == "buy" else "buy"
    return _px(_slipped(price, side, slippage_bps))


def _cash_pnl(
    side: Side,
    qty: Decimal,
    entry: Decimal,
    exit_px: Decimal,
    entry_fee: Decimal,
    exit_fee: Decimal,
) -> Decimal:
    if side == "buy":
        gross = q_cash(qty * (exit_px - entry))
    else:
        gross = q_cash(qty * (entry - exit_px))
    return q_cash(gross - entry_fee - exit_fee)


@dataclass
class _Lot:
    rung: GridRung
    qty: Decimal
    entry: Decimal
    entry_fee: Decimal
    entry_index: int
    entry_ts: datetime
    range_kind: str
    lower: float
    upper: float


class _Book:
    def __init__(self, params: GridParams, qty: Decimal, buying_power: Decimal) -> None:
        self.params = params
        self.qty = qty
        self.buying_power = buying_power
        self.lots: list[_Lot] = []
        self.trades: list[GridTrade] = []
        self.realized = Decimal("0")
        self.opens = 0

    def flatten(self, index: int, candle: Candle, price: float, reason: GridExit) -> None:
        for lot in list(self.lots):
            self._close(lot, index, candle, price, reason)

    def take_profits(self, index: int, candle: Candle) -> bool:
        closed = False
        for lot in list(self.lots):
            if lot.entry_index >= index:
                continue
            if not _target_hit(candle, lot.rung.side, lot.rung.target):
                continue
            self._close(lot, index, candle, lot.rung.target, "target")
            closed = True
        return closed

    def enter(self, index: int, candle: Candle, bracket: Bracket) -> None:
        held = self.lots[0].rung.side if self.lots else None
        for side in ("buy", "sell"):
            if held is not None and held != side:
                continue
            count = sum(1 for lot in self.lots if lot.rung.side == side)
            for rung in bracket.rungs:
                if rung.side != side:
                    continue
                if count >= self.params.max_inventory_units:
                    break
                if any(lot.rung.side == side and lot.rung.k == rung.k for lot in self.lots):
                    continue
                if not _crossed(candle, side, rung.price):
                    continue
                if _target_hit(candle, side, rung.target):
                    continue
                if not self._open(index, candle, bracket, rung):
                    break
                count += 1
                held = side

    def equity(self, close: float) -> Decimal:
        total = self.realized
        for lot in self.lots:
            exit_px = _fill(close, lot.rung.side, False, self.params.slippage_bps)
            total += _cash_pnl(
                lot.rung.side,
                lot.qty,
                lot.entry,
                exit_px,
                lot.entry_fee,
                _fee(lot.qty, exit_px, self.params.fee_bps),
            )
        return q_cash(total)

    def _open(self, index: int, candle: Candle, bracket: Bracket, rung: GridRung) -> bool:
        fill = _fill(rung.price, rung.side, True, self.params.slippage_bps)
        fee = _fee(self.qty, fill, self.params.fee_bps)
        margin = q_cash(self.qty * fill)
        if margin + fee > self._free():
            return False
        self.lots.append(
            _Lot(
                rung=rung,
                qty=self.qty,
                entry=fill,
                entry_fee=fee,
                entry_index=index,
                entry_ts=candle.open_ts,
                range_kind=bracket.kind,
                lower=bracket.lower,
                upper=bracket.upper,
            )
        )
        self.opens += 1
        return True

    def _close(self, lot: _Lot, index: int, candle: Candle, price: float, reason: GridExit) -> None:
        exit_px = _fill(price, lot.rung.side, False, self.params.slippage_bps)
        exit_fee = _fee(lot.qty, exit_px, self.params.fee_bps)
        pnl = _cash_pnl(lot.rung.side, lot.qty, lot.entry, exit_px, lot.entry_fee, exit_fee)
        self.realized = q_cash(self.realized + pnl)
        self.lots.remove(lot)
        self.trades.append(
            GridTrade(
                side=lot.rung.side,
                entry_index=lot.entry_index,
                exit_index=index,
                entry_ts=lot.entry_ts,
                exit_ts=candle.open_ts,
                entry=lot.entry,
                exit=exit_px,
                pnl=pnl,
                reason=reason,
                range_kind=lot.range_kind,
                lower=lot.lower,
                upper=lot.upper,
            )
        )

    def _free(self) -> Decimal:
        locked = Decimal("0")
        for lot in self.lots:
            locked += q_cash(lot.qty * lot.entry) + lot.entry_fee
        return q_cash(self.buying_power + self.realized - locked)
