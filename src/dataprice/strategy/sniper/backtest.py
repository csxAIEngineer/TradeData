"""一击必杀 v0 的回测。判断本身不读库。

同时只持有一笔。进场在信号那根 4H 的收盘：回踩订单块或公允价值缺口，收盘站回有利的一侧。
从下一根才看出场。同一根先碰到止损就按止损走，不再给目标。
GATE=0 只是不开新仓，不平掉手里的单。偏向翻掉也不平。
数据走到头还没打到止损或目标，按最后一根收盘记一笔 ``end``，这是回测收尾，不是规则出场。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from dataprice.config import Settings
from dataprice.contracts import Side, q_px
from dataprice.strategy.candles import Candle, assert_sorted
from dataprice.strategy.sniper.bias import DailyBias, bias_for_session, daily_bias
from dataprice.strategy.sniper.entry import Zone, find_retest, liquidity_zones, stop_beyond_wick
from dataprice.strategy.sniper.macro_gate import EffectiveGate, GateCalendar, MacroBook
from dataprice.strategy.sniper.params import SniperParams, evaluated_params
from dataprice.strategy.sniper.resample import to_4h, to_daily
from dataprice.strategy.sniper.risk import TradePlan, fill_price, plan_trade, trade_pnl
from dataprice.timeutil import UTC, ensure_aware

ExitReason = Literal["stop", "target", "end"]


@dataclass(frozen=True)
class SniperTrade:
    side: Side
    entry_index: int
    exit_index: int
    entry_ts: datetime
    exit_ts: datetime
    entry: Decimal
    exit: Decimal
    qty: Decimal
    stop: Decimal
    target: Decimal
    reason: ExitReason
    pnl: Decimal
    price_r: float | None
    account_r: float | None
    planned_price_r: float
    planned_account_r: float
    leverage_used: float
    bias: Literal["long", "short"]


@dataclass(frozen=True)
class SniperResult:
    trades: tuple[SniperTrade, ...]
    bars: int
    skipped: int
    first_open: datetime
    last_open: datetime
    first_close: float
    last_close: float


def run_sniper(
    bars_4h: Sequence[Candle],
    daily: Sequence[Candle],
    params: SniperParams | None = None,
    *,
    macro: MacroBook | None = None,
    gates: Mapping[date, EffectiveGate] | None = None,
    trade_start: datetime | None = None,
) -> SniperResult:
    """``gates`` 优先。否则用 ``macro`` 按 v0.1 现算。两个都没有就不能跑。"""
    params = params or evaluated_params()
    if not bars_4h:
        raise ValueError("没有 4H K 线，不能做一击必杀")
    assert_sorted(bars_4h)
    biases = daily_bias(daily, params)
    calendar = _gate_map(bars_4h, macro, gates, params)
    if trade_start is not None:
        ensure_aware(trade_start)
    trades: list[SniperTrade] = []
    position: _Position | None = None
    skipped = 0
    zones = liquidity_zones(bars_4h)
    for index, bar in enumerate(bars_4h):
        if position is not None:
            closed = _advance(position, bar, index, params)
            if closed is not None:
                trades.append(closed)
                position = None
            continue
        if trade_start is not None and bar.open_ts < trade_start:
            continue
        opened, skipped_one = _try_open(bars_4h, index, biases, calendar, params, zones)
        skipped += skipped_one
        position = opened
    if position is not None:
        last = len(bars_4h) - 1
        trades.append(_close_at(position, bars_4h[last], last, "end", params))
    return SniperResult(
        trades=tuple(trades),
        bars=len(bars_4h),
        skipped=skipped,
        first_open=bars_4h[0].open_ts,
        last_open=bars_4h[-1].open_ts,
        first_close=bars_4h[0].close,
        last_close=bars_4h[-1].close,
    )


def run_sniper_intraday(
    candles: Sequence[Candle],
    params: SniperParams | None = None,
    *,
    bar_minutes: int,
    macro: MacroBook | None = None,
    gates: Mapping[date, EffectiveGate] | None = None,
    trade_start: datetime | None = None,
) -> SniperResult:
    """从 30 分钟或 60 分钟合成 4H 和 UTC 日线，再跑 v0。"""
    if bar_minutes not in (30, 60):
        raise ValueError("一击必杀只从 30m 或 60m 合成 4H")
    bars_4h = to_4h(candles, bar_minutes=bar_minutes)
    daily = to_daily(candles, bar_minutes=bar_minutes)
    if not bars_4h:
        raise ValueError("这些 K 线合成不出完整的 4H")
    return run_sniper(
        bars_4h,
        daily,
        params,
        macro=macro,
        gates=gates,
        trade_start=trade_start,
    )


def run_stored_sniper(
    *,
    symbol: str = "BTC-USDT",
    start: datetime,
    end: datetime,
    params: SniperParams | None = None,
    settings: Settings | None = None,
) -> SniperResult:
    """从 ClickHouse 读 60 分钟和宏观日频。K 线多取 120 天，宏观多取 400 天，交易仍从 ``start`` 起。"""
    from datetime import timedelta

    from dataprice.market.repo import market_session
    from dataprice.strategy.candles import candles_from_frame
    from dataprice.strategy.sniper.store import book_from_frame, gates_from_frame

    params = params or evaluated_params()
    ensure_aware(start)
    ensure_aware(end)
    price_start = start - timedelta(days=120)
    macro_start = (start - timedelta(days=400)).date()
    with market_session(settings) as repo:
        frame = repo.query_bars([symbol], "60m", price_start, end)
        macro_frame = repo.query_macro_daily(macro_start, end.date())
        book = book_from_frame(macro_frame)
        stored = ()
        if book.is_empty():
            stored = gates_from_frame(
                repo.query_factor_macro_gate(params.macro_version, macro_start, end.date())
            )
    if not book.is_empty():
        macro: MacroBook | None = book
        gates = None
    elif stored:
        macro = None
        gates = {row.effective_date: row for row in stored}
    else:
        raise RuntimeError(
            "没有宏观日频，也没有 factor_macro_gate v0.1。"
            "先执行 uv run dataprice macro-backfill --csv <文件>，"
            "或 uv run dataprice macro-backfill --fetch"
        )
    candles = tuple(candles_from_frame(frame))
    if not candles:
        raise RuntimeError(
            f"ClickHouse 里没有 {symbol} 60m。先执行: "
            f"uv run dataprice backfill --symbol {symbol} --freq 60m"
        )
    return run_sniper_intraday(
        candles,
        params,
        bar_minutes=60,
        macro=macro,
        gates=gates,
        trade_start=start,
    )


@dataclass
class _Position:
    plan: TradePlan
    entry_index: int
    entry_ts: datetime
    bias: Literal["long", "short"]


def _try_open(
    bars: Sequence[Candle],
    index: int,
    biases: Sequence[DailyBias],
    calendar: Mapping[date, EffectiveGate],
    params: SniperParams,
    zones: Sequence[Zone],
) -> tuple[_Position | None, int]:
    if index + 1 >= len(bars):
        return None, 0
    bar = bars[index]
    session = bar.open_ts.astimezone(UTC).date()
    gate = calendar.get(session)
    if gate is None or gate.gate != 1:
        return None, 0
    bias = bias_for_session(biases, session)
    if bias is None or bias.bias == "flat":
        return None, 0
    signal = find_retest(bars, index, zones)
    if signal is None:
        return None, 0
    if signal.side == "buy" and bias.bias != "long":
        return None, 0
    if signal.side == "sell" and bias.bias != "short":
        return None, 0
    raw_stop = stop_beyond_wick(signal.side, signal.wick, params.stop_buffer_bps)
    plan = plan_trade(signal.side, bar.close, raw_stop, params)
    if plan is None:
        return None, 1
    side_bias: Literal["long", "short"] = "long" if signal.side == "buy" else "short"
    return (
        _Position(plan=plan, entry_index=index, entry_ts=bar.open_ts, bias=side_bias),
        0,
    )


def _advance(position: _Position, bar: Candle, index: int, params: SniperParams) -> SniperTrade | None:
    if index <= position.entry_index:
        return None
    plan = position.plan
    if _stopped(plan, bar):
        raw = bar.open if _gapped_stop(plan, bar) else float(plan.stop)
        return _close_at(position, bar, index, "stop", params, raw)
    if _targeted(plan, bar):
        return _close_at(position, bar, index, "target", params, float(plan.target))
    return None


def _close_at(
    position: _Position,
    bar: Candle,
    index: int,
    reason: ExitReason,
    params: SniperParams,
    raw: float | None = None,
) -> SniperTrade:
    plan = position.plan
    exit_side: Side = "sell" if plan.side == "buy" else "buy"
    price = bar.close if raw is None else raw
    exit_px = fill_price(price, exit_side, params.slippage_bps)
    pnl = trade_pnl(plan.side, plan.qty, plan.entry, exit_px, params.fee_bps)
    move = exit_px - plan.entry if plan.side == "buy" else plan.entry - exit_px
    price_r = float(move / plan.price_risk) if plan.price_risk > 0 else None
    account_r = float(pnl / plan.risk_cash) if plan.risk_cash > 0 else None
    return SniperTrade(
        side=plan.side,
        entry_index=position.entry_index,
        exit_index=index,
        entry_ts=position.entry_ts,
        exit_ts=bar.open_ts,
        entry=plan.entry,
        exit=q_px(exit_px),
        qty=plan.qty,
        stop=plan.stop,
        target=plan.target,
        reason=reason,
        pnl=pnl,
        price_r=price_r,
        account_r=account_r,
        planned_price_r=plan.planned_price_r,
        planned_account_r=plan.planned_account_r,
        leverage_used=plan.leverage_used,
        bias=position.bias,
    )


def _stopped(plan: TradePlan, bar: Candle) -> bool:
    stop = float(plan.stop)
    if plan.side == "buy":
        return bar.open <= stop or bar.low <= stop
    return bar.open >= stop or bar.high >= stop


def _gapped_stop(plan: TradePlan, bar: Candle) -> bool:
    stop = float(plan.stop)
    if plan.side == "buy":
        return bar.open <= stop
    return bar.open >= stop


def _targeted(plan: TradePlan, bar: Candle) -> bool:
    target = float(plan.target)
    if plan.side == "buy":
        return bar.high >= target
    return bar.low <= target


def _gate_map(
    bars: Sequence[Candle],
    macro: MacroBook | None,
    gates: Mapping[date, EffectiveGate] | None,
    params: SniperParams,
) -> dict[date, EffectiveGate]:
    """只算这些 4H 落在的会话日。没有更早宏观依据的日子不放进表，调用方当关门。"""
    if gates is not None:
        return dict(gates)
    if macro is None:
        raise ValueError("需要宏观序列，或已经算好的 factor_macro_gate")
    calendar = GateCalendar(macro, params)
    found: dict[date, EffectiveGate] = {}
    for bar in bars:
        session = bar.open_ts.astimezone(UTC).date()
        if session in found:
            continue
        row = calendar.on(session)
        if row is not None:
            found[session] = row
    return found
