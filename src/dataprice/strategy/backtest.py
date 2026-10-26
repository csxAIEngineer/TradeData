"""用 30m 信号做一笔一笔的回测。读行情在调用方，这里只吃 K 线和信号。

多头开平会走过 ``hotpath.TradingState``。空头开仓不能走同一条路径：
``evaluate`` 只允许卖出已经持有的多头。空头仍用同一套停机、名义和购买力限额，
现金占用单独记，结束后购买力回到同一套公式。

成交在信号那根的收盘价。止损和止盈从下一根开始看。同一根先碰到止损
（跳空则按开盘价）就按止损出，不再给止盈。``exit_on_bias_flip`` 为真时，
1 小时偏向不再同向就按收盘价退出。``breakeven_at_r`` 在价格先走出这么多 R
的下一根，把止损改到进场价。``slippage_bps`` 让每一笔成交都往不利方向滑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Sequence

from dataprice.config import Settings
from dataprice.contracts import Side, q_cash, q_px, q_qty
from dataprice.hotpath.state import OrderIntent, TradingState
from dataprice.strategy.candles import Candle
from dataprice.strategy.flow import TakerBar
from dataprice.strategy.signals import Signal, SignalBook, StrategyParams, generate_signals

ExitReason = Literal["stop", "target", "bias_flip", "end", "mode"]


@dataclass(frozen=True)
class Trade:
    side: Side
    entry_index: int
    exit_index: int
    entry_ts: datetime
    exit_ts: datetime
    entry: Decimal
    exit: Decimal
    stop: float
    target: float
    reason: ExitReason
    pnl: Decimal
    zone_kind: str


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Trade, ...]
    signals: int
    starting_buying_power: Decimal
    ending_buying_power: Decimal
    open_side: Side | None = None
    open_entry: Decimal | None = None

    @property
    def realized_pnl(self) -> Decimal:
        total = Decimal("0")
        for trade in self.trades:
            total += trade.pnl
        return q_cash(total)


@dataclass
class _Position:
    symbol: str
    side: Side
    qty: Decimal
    entry: Decimal
    entry_fee: Decimal
    stop: float
    target: float
    entry_index: int
    entry_ts: datetime
    zone_kind: str
    bp_before: Decimal
    mirrored: bool
    initial_stop: float
    be_ready: bool = False


def run_backtest(
    candles_30: Sequence[Candle],
    candles_60: Sequence[Candle],
    params: StrategyParams | None = None,
    *,
    symbol: str = "BTC-USDT",
    qty: Decimal = Decimal("0.01"),
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    buying_power: Decimal = Decimal("1000000"),
    max_order_notional: Decimal = Decimal("1000000"),
    max_position_qty: Decimal = Decimal("10"),
    flow: Sequence[TakerBar] | None = None,
) -> BacktestResult:
    params = params or StrategyParams()
    book = generate_signals(candles_30, candles_60, params, flow=flow)
    return simulate(
        candles_30,
        book,
        symbol=symbol,
        qty=qty,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        buying_power=buying_power,
        max_order_notional=max_order_notional,
        max_position_qty=max_position_qty,
        exit_on_bias_flip=params.exit_on_bias_flip,
        breakeven_at_r=params.breakeven_at_r,
    )


def simulate(
    candles: Sequence[Candle],
    book: SignalBook,
    *,
    symbol: str = "BTC-USDT",
    qty: Decimal = Decimal("0.01"),
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    buying_power: Decimal = Decimal("1000000"),
    max_order_notional: Decimal = Decimal("1000000"),
    max_position_qty: Decimal = Decimal("10"),
    exit_on_bias_flip: bool = True,
    breakeven_at_r: float | None = None,
    force_flat: Sequence[bool] | None = None,
) -> BacktestResult:
    """``force_flat[i]`` 为真时，这根开盘价平掉上一模式留下的仓，再考虑这根的新信号。

    默认不传，止损、止盈和偏向翻转照旧。日线分配用它在模式切换的第一根交接。
    """
    if len(book.bias_1h) != len(candles):
        raise ValueError("1h 偏向长度必须和 30m K 线一致")
    if force_flat is not None and len(force_flat) != len(candles):
        raise ValueError("force_flat 长度必须和 K 线一致")
    state = TradingState()
    state.load_snapshot(
        positions=[],
        halted=False,
        buying_power=buying_power,
        max_order_notional=max_order_notional,
        max_position_qty=max_position_qty,
    )
    assert state.risk is not None
    starting = state.risk.buying_power
    qty = q_qty(qty)
    by_index = {signal.index: signal for signal in book.signals}
    position: _Position | None = None
    trades: list[Trade] = []
    last = len(candles) - 1
    for index, candle in enumerate(candles):
        if position is not None:
            _arm_breakeven(position)
            closing = index == last and index > position.entry_index
            if force_flat is not None and force_flat[index] and index > position.entry_index:
                outcome = ("mode", candle.open)
            else:
                outcome = _exit_price(
                    candle,
                    position,
                    book.bias_1h[index],
                    index,
                    exit_on_bias_flip=exit_on_bias_flip,
                )
            if outcome is None and closing:
                outcome = ("end", candle.close)
            if outcome is not None:
                reason, price = outcome
                trades.append(
                    _close(
                        state,
                        position,
                        index,
                        candle.open_ts,
                        price,
                        reason,
                        fee_bps,
                        slippage_bps,
                    )
                )
                position = None
            else:
                _mark_breakeven(position, candle, breakeven_at_r)
        if position is None and index in by_index and index < last:
            position = _open(state, by_index[index], symbol, qty, fee_bps, slippage_bps)
    open_side = position.side if position is not None else None
    open_entry = position.entry if position is not None else None
    assert state.risk is not None
    return BacktestResult(
        trades=tuple(trades),
        signals=len(book.signals),
        starting_buying_power=starting,
        ending_buying_power=state.risk.buying_power,
        open_side=open_side,
        open_entry=open_entry,
    )


def format_report(result: BacktestResult, *, split: datetime | None = None) -> str:
    stats = performance(result)
    lines = [
        f"信号 {result.signals}，平仓 {stats.trades}，胜率 {stats.win_rate:.1%}，盈亏比 {stats.profit_factor:.2f}",
        f"已实现盈亏 {result.realized_pnl}，最大回撤 {stats.max_drawdown}，平均持有 {stats.avg_hold_bars:.1f} 根",
        f"购买力 {result.starting_buying_power} → {result.ending_buying_power}",
    ]
    if split is not None:
        lines.append(_window_line("训练", result, lambda trade: trade.entry_ts < split))
        lines.append(_window_line("留出", result, lambda trade: trade.entry_ts >= split))
    if result.open_side is not None:
        lines.append(f"未平仓 {result.open_side} @ {result.open_entry}")
    return "\n".join(lines)


def _window_line(label: str, result: BacktestResult, predicate) -> str:
    trades = tuple(trade for trade in result.trades if predicate(trade))
    stats = performance(
        BacktestResult(
            trades=trades,
            signals=result.signals,
            starting_buying_power=result.starting_buying_power,
            ending_buying_power=result.ending_buying_power,
        )
    )
    buys = [trade for trade in trades if trade.side == "buy"]
    sells = [trade for trade in trades if trade.side == "sell"]
    buy_pnl = q_cash(sum((trade.pnl for trade in buys), Decimal("0")))
    sell_pnl = q_cash(sum((trade.pnl for trade in sells), Decimal("0")))
    return (
        f"{label} 平仓 {stats.trades}，盈亏 {stats.pnl}，胜率 {stats.win_rate:.1%}，"
        f"盈亏比 {stats.profit_factor:.2f}，平均R {stats.avg_r:+.2f}，回撤 {stats.max_drawdown}，"
        f"多 {len(buys)}/{buy_pnl}，空 {len(sells)}/{sell_pnl}"
    )


@dataclass(frozen=True)
class Performance:
    trades: int
    pnl: Decimal
    win_rate: float
    profit_factor: float
    max_drawdown: Decimal
    avg_hold_bars: float
    avg_r: float


def performance(result: BacktestResult) -> Performance:
    """已平仓交易的汇总。盈亏比是盈利合计除以亏损绝对值，没有亏损时为无穷。"""
    wins = [trade.pnl for trade in result.trades if trade.pnl > 0]
    losses = [trade.pnl for trade in result.trades if trade.pnl < 0]
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
    max_dd = Decimal("0")
    r_total = 0.0
    r_count = 0
    holds = 0
    for trade in result.trades:
        equity += trade.pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        holds += trade.exit_index - trade.entry_index
        multiple = _r_multiple(trade)
        if multiple is not None:
            r_total += multiple
            r_count += 1
    count = len(result.trades)
    return Performance(
        trades=count,
        pnl=result.realized_pnl,
        win_rate=(len(wins) / count) if count else 0.0,
        profit_factor=factor,
        max_drawdown=q_cash(max_dd),
        avg_hold_bars=(holds / count) if count else 0.0,
        avg_r=(r_total / r_count) if r_count else 0.0,
    )


def _r_multiple(trade: Trade) -> float | None:
    entry = float(trade.entry)
    exit_ = float(trade.exit)
    risk = (entry - trade.stop) if trade.side == "buy" else (trade.stop - entry)
    if risk <= 0:
        return None
    move = (exit_ - entry) if trade.side == "buy" else (entry - exit_)
    return move / risk


def _open(
    state: TradingState,
    signal: Signal,
    symbol: str,
    qty: Decimal,
    fee_bps: float,
    slippage_bps: float,
) -> _Position | None:
    assert state.risk is not None
    price = _px(_slipped(signal.entry, signal.side, slippage_bps))
    fee = _fee(qty, price, fee_bps)
    before = state.risk.buying_power
    if signal.side == "buy":
        intent = OrderIntent(symbol=symbol, side="buy", qty=qty, price=price, fee=fee)
        if not state.evaluate(intent).accepted:
            return None
        state.apply_fill(intent)
        mirrored = True
    else:
        if not _short_allowed(state, qty, price, fee):
            return None
        margin = q_cash(qty * price)
        state.risk.buying_power = q_cash(before - margin - fee)
        mirrored = False
    return _Position(
        symbol=symbol,
        side=signal.side,
        qty=qty,
        entry=price,
        entry_fee=fee,
        stop=signal.stop,
        target=signal.target,
        entry_index=signal.index,
        entry_ts=signal.open_ts,
        zone_kind=signal.zone_kind,
        bp_before=before,
        mirrored=mirrored,
        initial_stop=signal.stop,
    )


def _close(
    state: TradingState,
    position: _Position,
    index: int,
    exit_ts: datetime,
    price: float,
    reason: ExitReason,
    fee_bps: float,
    slippage_bps: float,
) -> Trade:
    assert state.risk is not None
    close_side: Side = "sell" if position.side == "buy" else "buy"
    exit_px = _px(_slipped(price, close_side, slippage_bps))
    fee = _fee(position.qty, exit_px, fee_bps)
    if position.mirrored:
        state.apply_fill(
            OrderIntent(
                symbol=position.symbol,
                side="sell",
                qty=position.qty,
                price=exit_px,
                fee=fee,
            )
        )
    else:
        gross = q_cash(position.qty * (position.entry - exit_px))
        pnl = q_cash(gross - position.entry_fee - fee)
        state.risk.buying_power = q_cash(position.bp_before + pnl)
    pnl = q_cash(state.risk.buying_power - position.bp_before)
    return Trade(
        side=position.side,
        entry_index=position.entry_index,
        exit_index=index,
        entry_ts=position.entry_ts,
        exit_ts=exit_ts,
        entry=position.entry,
        exit=exit_px,
        stop=position.stop,
        target=position.target,
        reason=reason,
        pnl=pnl,
        zone_kind=position.zone_kind,
    )


def _exit_price(
    candle: Candle,
    position: _Position,
    bias: str,
    index: int,
    *,
    exit_on_bias_flip: bool,
) -> tuple[ExitReason, float] | None:
    if index <= position.entry_index:
        return None
    if position.side == "buy":
        if candle.open <= position.stop:
            return ("stop", candle.open)
        if candle.low <= position.stop:
            return ("stop", position.stop)
        if candle.high >= position.target:
            return ("target", position.target)
        if exit_on_bias_flip and bias != "bull":
            return ("bias_flip", candle.close)
        return None
    if candle.open >= position.stop:
        return ("stop", candle.open)
    if candle.high >= position.stop:
        return ("stop", position.stop)
    if candle.low <= position.target:
        return ("target", position.target)
    if exit_on_bias_flip and bias != "bear":
        return ("bias_flip", candle.close)
    return None


def _arm_breakeven(position: _Position) -> None:
    """上一根已经走出 breakeven_at_r，这一根开始用进场价当止损。"""
    if not position.be_ready:
        return
    entry = float(position.entry)
    if position.side == "buy" and position.stop < entry:
        position.stop = entry
    elif position.side == "sell" and position.stop > entry:
        position.stop = entry
    position.be_ready = False


def _mark_breakeven(position: _Position, candle: Candle, breakeven_at_r: float | None) -> None:
    if breakeven_at_r is None:
        return
    entry = float(position.entry)
    risk = abs(entry - position.initial_stop)
    if risk <= 0:
        return
    if position.side == "buy" and candle.high >= entry + risk * breakeven_at_r:
        position.be_ready = True
    elif position.side == "sell" and candle.low <= entry - risk * breakeven_at_r:
        position.be_ready = True


def _short_allowed(state: TradingState, qty: Decimal, price: Decimal, fee: Decimal) -> bool:
    """空头不能记进 hotpath 持仓，开仓前仍检查停机、名义和购买力。"""
    if state.risk is None or state.risk.halted:
        return False
    notional = q_cash(qty * price)
    if notional > state.risk.max_order_notional:
        return False
    if notional + fee > state.risk.buying_power:
        return False
    return True


def run_stored_backtest(
    *,
    symbol: str = "BTC-USDT",
    start: datetime,
    end: datetime,
    params: StrategyParams | None = None,
    qty: Decimal = Decimal("0.01"),
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    settings: Settings | None = None,
) -> tuple[BacktestResult, int, int]:
    """从 ClickHouse ``market.bars`` 读 30m / 60m，再跑回测。

    ``params.min_flow_imbalance`` 不为空时，再读 ``market.taker_flow``。
    """
    from dataprice.feed.binance_flow import BINANCE_UM_SOURCE
    from dataprice.market.repo import market_session
    from dataprice.strategy.candles import candles_from_frame
    from dataprice.strategy.flow import taker_bars_from_rows

    params = params or StrategyParams()
    with market_session(settings) as repo:
        frame_30 = repo.query_bars([symbol], "30m", start, end)
        frame_60 = repo.query_bars([symbol], "60m", start, end)
        flow_frame = None
        if params.min_flow_imbalance is not None:
            flow_frame = repo.query_taker_flow(
                [symbol],
                "30m",
                start,
                end,
                source=BINANCE_UM_SOURCE,
            )
    candles_30 = candles_from_frame(frame_30)
    candles_60 = candles_from_frame(frame_60)
    if not candles_30 or not candles_60:
        raise RuntimeError(
            "ClickHouse 里没有足够的 K 线。先执行: "
            f"uv run dataprice backfill --symbol {symbol}"
        )
    flow = None
    if flow_frame is not None:
        flow = taker_bars_from_rows(flow_frame.itertuples(index=False))
        if not flow:
            raise RuntimeError(
                "ClickHouse 里没有主动成交。先执行: "
                f"uv run dataprice flow-backfill --symbol {symbol}"
            )
    result = run_backtest(
        candles_30,
        candles_60,
        params,
        symbol=symbol,
        qty=qty,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        flow=flow,
    )
    return result, len(candles_30), len(candles_60)


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
