"""日线辩论分配。阈值锁定，不用留出样本再调。

每一根已经收盘的 UTC 日线，只用收盘时已经知道的特征，给下一段交易指定
一个模式。这段从该日收盘一直到下一根日线收盘，中间不再改。

1. 已经收盘的 4 小时 K 线、18 根收益的 Kaufman 效率比 >= 0.25：
   4 小时结构偏多 → ``long``（``long_params()``，只做多，CLV >= 0.70）
   偏空 → ``short``（``short_regime_params()`` 整条路径）
   中性或还没有结构 → ``flat``
2. 效率比 < 0.25：
   已确认的等高/等低能放下至少一档约 50 个基点的网格 → ``grid``
   弱区间（放不进这一档，或只能退回 48 根上下沿）或没有区间 → ``flat``
3. 同时出现多个可执行主张，或特征对不上 → ``flat``

``--long``、``--short-regime``、``--grid`` 各自的默认参数不改。
这里只决定哪一天把哪一条放出来。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Literal, Sequence

from dataprice.config import Settings
from dataprice.contracts import q_cash
from dataprice.strategy.backtest import simulate
from dataprice.strategy.candles import Candle, aggregate_candles, assert_sorted, bar_delta
from dataprice.strategy.grid import (
    HOLDOUT_START,
    GridParams,
    GridResult,
    active_bracket,
    grid_windows,
    simulate_grid,
)
from dataprice.strategy.liquidity import Level, LiquidityParams, map_liquidity
from dataprice.strategy.regime import efficiency_asof
from dataprice.strategy.signals import (
    Signal,
    SignalBook,
    bias_asof,
    generate_signals,
    long_params,
    short_regime_params,
)
from dataprice.strategy.structure import structure_path

Mode = Literal["long", "short", "grid", "flat"]
MODES: tuple[Mode, ...] = ("long", "short", "grid", "flat")

# 与三条猎手训练时锁定的门槛相同。留出样本不参与，也不在这里改。
ER_THRESHOLD = 0.25
ER_BARS = 18


@dataclass(frozen=True)
class Decision:
    """``close_ts`` 是做出决定的那根日线的收盘时刻。模式用于此后、直到下一根日线收盘。"""

    close_ts: datetime
    mode: Mode
    er: float | None
    bias_4h: str | None
    strong_range: bool


@dataclass(frozen=True)
class ClosedTrade:
    mode: str
    side: str
    entry_ts: datetime
    exit_ts: datetime
    pnl: Decimal


@dataclass(frozen=True)
class DebateResult:
    decisions: tuple[Decision, ...]
    modes: tuple[str, ...]
    trades: tuple[ClosedTrade, ...]
    grid_result: GridResult
    covered: tuple[bool, ...]


@dataclass(frozen=True)
class Bucket:
    window: str
    mode: str
    days: int
    trades: int
    pnl: Decimal
    win_rate: float
    profit_factor: float
    max_drawdown: Decimal


def resolve_claims(claims: Sequence[str]) -> Mode:
    """恰好一个可执行主张才开仓。多个主张、主张和空仓打架、或一个都没有，都是 flat。"""
    actionable: list[str] = []
    saw_flat = False
    for claim in claims:
        if claim not in ("long", "short", "grid", "flat"):
            return "flat"
        if claim == "flat":
            saw_flat = True
            continue
        if claim not in actionable:
            actionable.append(claim)
    if saw_flat or len(actionable) != 1:
        return "flat"
    return actionable[0]  # type: ignore[return-value]


def resolve(*, er: float | None, bias_4h: str | None, strong_range: bool) -> Mode:
    """按锁定规则把当日特征收成一个模式。不接受门槛参数。"""
    if bias_4h not in (None, "bull", "bear", "neutral"):
        return "flat"
    if er is None or (isinstance(er, float) and math.isnan(er)):
        return "flat"
    claims: list[str] = []
    if er >= ER_THRESHOLD and bias_4h == "bull":
        claims.append("long")
    if er >= ER_THRESHOLD and bias_4h == "bear":
        claims.append("short")
    if er < ER_THRESHOLD and strong_range:
        claims.append("grid")
    return resolve_claims(claims)


def is_strong_range(
    candles: Sequence[Candle],
    levels: Sequence[Level],
    index: int,
    params: GridParams | None = None,
) -> bool:
    """上一根收盘价两侧有已确认的 eqh/eql，并且放得下至少一档网格。

    网格自己在没有这对水位时会退回最近上下沿。那一档对分配来说是弱区间，不算。
    """
    params = params or GridParams()
    bracket = active_bracket(candles, levels, index, params)
    return bracket is not None and bracket.kind == "eq"


def decisions_asof(
    candles_30: Sequence[Candle],
    candles_60: Sequence[Candle],
    params: GridParams | None = None,
) -> tuple[Decision, ...]:
    """每根已收盘日线一条决定。不够 18 根 4 小时收益时效率比是 None，模式为 flat。"""
    params = params or GridParams()
    if not candles_30 or not candles_60:
        return ()
    assert_sorted(candles_30)
    assert_sorted(candles_60)
    daily = aggregate_candles(candles_60, "1d")
    candles_4h = aggregate_candles(candles_60, "4h")
    if not daily:
        return ()
    er_at = efficiency_asof(candles_4h, "4h", daily, "1d", ER_BARS)
    path_4h = structure_path(candles_4h, params.wing)
    bias_at = bias_asof(candles_4h, path_4h.bias, "4h", daily, "1d")
    swings = structure_path(candles_30, params.wing).swings
    levels = map_liquidity(
        candles_30,
        swings,
        LiquidityParams(eq_bps=params.eq_bps, eq_lookback=params.eq_lookback),
    ).levels
    out: list[Decision] = []
    for index, day in enumerate(daily):
        close_ts = day.open_ts + bar_delta("1d")
        bar_index = _first_index_at_or_after(candles_30, close_ts)
        strong = (
            False
            if bar_index is None
            else is_strong_range(candles_30, levels, bar_index, params)
        )
        out.append(
            Decision(
                close_ts=close_ts,
                mode=resolve(er=er_at[index], bias_4h=bias_at[index], strong_range=strong),
                er=er_at[index],
                bias_4h=bias_at[index],
                strong_range=strong,
            )
        )
    return tuple(out)


def modes_for(candles: Sequence[Candle], decisions: Sequence[Decision]) -> tuple[str, ...]:
    """决定只作用于收盘之后的 K 线。下一根日线收盘前，模式不变。"""
    modes: list[str] = ["flat"] * len(candles)
    ordered = sorted(decisions, key=lambda item: item.close_ts)
    if not ordered:
        return tuple(modes)
    cursor = 0
    for index, candle in enumerate(candles):
        while cursor + 1 < len(ordered) and ordered[cursor + 1].close_ts <= candle.open_ts:
            cursor += 1
        if ordered[cursor].close_ts <= candle.open_ts:
            modes[index] = ordered[cursor].mode
    return tuple(modes)


def select_signals(
    long_signals: Sequence[Signal],
    short_signals: Sequence[Signal],
    modes: Sequence[str],
) -> tuple[Signal, ...]:
    """多头日只用多头路径的信号，空头日只用空头路径的信号。同一根有两笔就都不做。"""
    chosen: dict[int, list[Signal]] = {}
    for signal in long_signals:
        if signal.index < len(modes) and modes[signal.index] == "long":
            chosen.setdefault(signal.index, []).append(signal)
    for signal in short_signals:
        if signal.index < len(modes) and modes[signal.index] == "short":
            chosen.setdefault(signal.index, []).append(signal)
    kept: list[Signal] = []
    for index in sorted(chosen):
        group = chosen[index]
        if len(group) == 1:
            kept.append(group[0])
    return tuple(kept)


def run_debate(
    candles_30: Sequence[Candle],
    candles_60: Sequence[Candle],
    *,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    qty: Decimal = Decimal("0.01"),
    buying_power: Decimal = Decimal("1000000"),
    grid_params: GridParams | None = None,
) -> DebateResult:
    """多头、空头、网格各跑各的默认路径，只在分配给它的日子里开仓。

    模式切换的那根 30 分钟，上一笔在开盘价平掉。网格在非网格日同样在开盘价平库存。
    网格日不再用盘中效率比把当天改回空仓；效率比只在日线收盘时决定模式。
    空头路径自己的进场过滤仍在，包括空头效率比至少 0.25。这不是改门槛。
    """
    grid_params = grid_params or GridParams(fee_bps=fee_bps, slippage_bps=slippage_bps)
    if (
        grid_params.fee_bps != fee_bps
        or grid_params.slippage_bps != slippage_bps
        or grid_params.unit_qty != qty
    ):
        grid_params = replace(
            grid_params,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            unit_qty=qty,
        )
    if long_params().exit_on_bias_flip != short_regime_params().exit_on_bias_flip:
        raise RuntimeError("多头和空头路径的偏向出场不一致，不能放进同一个回测")
    if long_params().breakeven_at_r != short_regime_params().breakeven_at_r:
        raise RuntimeError("多头和空头路径的保本规则不一致，不能放进同一个回测")
    decisions = decisions_asof(candles_30, candles_60, grid_params)
    modes = modes_for(candles_30, decisions)
    covered = _covered(candles_30, decisions)
    long_book = generate_signals(candles_30, candles_60, long_params())
    short_book = generate_signals(candles_30, candles_60, short_regime_params())
    signals = select_signals(long_book.signals, short_book.signals, modes)
    book = SignalBook(
        candles=tuple(candles_30),
        signals=signals,
        bias_30m=long_book.bias_30m,
        bias_1h=long_book.bias_1h,
    )
    force_flat = [False] * len(candles_30)
    for index in range(1, len(modes)):
        if modes[index] != modes[index - 1]:
            force_flat[index] = True
    ict = simulate(
        candles_30,
        book,
        qty=qty,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        buying_power=buying_power,
        exit_on_bias_flip=long_params().exit_on_bias_flip,
        breakeven_at_r=long_params().breakeven_at_r,
        force_flat=force_flat,
    )
    candles_4h = aggregate_candles(candles_60, "4h")
    er_at = efficiency_asof(candles_4h, "4h", candles_30, "30m", grid_params.er_bars)
    enabled = [mode == "grid" for mode in modes]
    grid = simulate_grid(
        candles_30,
        er_at,
        grid_params,
        buying_power=buying_power,
        enabled=enabled,
    )
    trades: list[ClosedTrade] = []
    for trade in ict.trades:
        trades.append(
            ClosedTrade(
                mode=modes[trade.entry_index],
                side=trade.side,
                entry_ts=trade.entry_ts,
                exit_ts=trade.exit_ts,
                pnl=trade.pnl,
            )
        )
    for trade in grid.trades:
        trades.append(
            ClosedTrade(
                mode=modes[trade.entry_index],
                side=trade.side,
                entry_ts=trade.entry_ts,
                exit_ts=trade.exit_ts,
                pnl=trade.pnl,
            )
        )
    trades.sort(key=lambda trade: (trade.exit_ts, trade.entry_ts, trade.mode))
    return DebateResult(
        decisions=decisions,
        modes=modes,
        trades=tuple(trades),
        grid_result=grid,
        covered=covered,
    )


def debate_buckets(
    result: DebateResult,
    *,
    holdout_start: datetime = HOLDOUT_START,
) -> tuple[Bucket, ...]:
    """训练是开仓早于 ``holdout_start``，留出是当天及之后。天数按决定时刻切。"""
    windows = (
        ("训练", None, holdout_start),
        ("留出", holdout_start, None),
        ("全窗口", None, None),
    )
    rows: list[Bucket] = []
    for name, start, end in windows:
        window_trades = [trade for trade in result.trades if _in_window(trade.entry_ts, start, end)]
        window_days = [
            decision
            for decision, flag in zip(result.decisions, result.covered)
            if flag and _in_window(decision.close_ts, start, end)
        ]
        rows.append(_bucket(name, "合计", window_days, window_trades))
        for mode in MODES:
            rows.append(
                _bucket(
                    name,
                    mode,
                    [decision for decision in window_days if decision.mode == mode],
                    [trade for trade in window_trades if trade.mode == mode],
                )
            )
    return tuple(rows)


def format_debate_report(
    result: DebateResult,
    *,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
) -> str:
    """训练 / 留出 / 全窗口，以及 long、short、grid、flat。回撤是已平仓盈亏。"""
    lines = [
        "日线分配：每根已收盘 UTC 日线指定下一日的模式，当天不再改。",
        f"效率比用 {ER_BARS} 根已收盘 4 小时收益，门槛 {ER_THRESHOLD:g}。"
        "不低于门槛时，4 小时结构偏多走 long_params（只做多，CLV>=0.70），"
        "偏空走 short_regime_params（多头仍是评估候选，空头另要效率比>=0.25），"
        "中性空仓。",
        "低于门槛时，只有已确认 eqh/eql 且放得下至少一档 50 bps 才走网格；"
        "退回 48 根上下沿算弱区间，空仓。多个主张同时成立也空仓。",
        f"费用：每边手续费 {fee_bps:g} bps，不利滑点 {slippage_bps:g} bps。"
        "成交按开仓 K 线的开盘时间切开，早于 2026-03-01 为训练。门槛没有用留出样本改过。",
        "最大回撤按该桶已平仓盈亏、出场时间累计，不是持仓盯市。",
        "| 样本 | 模式 | 天数 | 成交 | 盈亏 | 胜率 | 盈亏比 | 最大回撤 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in debate_buckets(result):
        factor = "inf" if row.profit_factor == float("inf") else f"{row.profit_factor:.2f}"
        lines.append(
            f"| {row.window} | {row.mode} | {row.days} | {row.trades} | "
            f"{_signed(row.pnl)} | {row.win_rate:.1%} | {factor} | {row.max_drawdown:.4f} |"
        )
    lines.append(_side_line("全窗口", result.trades))
    lines.append(
        _side_line("训练", [trade for trade in result.trades if trade.entry_ts < HOLDOUT_START])
    )
    lines.append(
        _side_line("留出", [trade for trade in result.trades if trade.entry_ts >= HOLDOUT_START])
    )
    train, holdout, full = grid_windows(result.grid_result)
    lines.append(
        "网格账本盯市回撤（库存浮盈浮亏，只含网格）："
        f"训练 {train.max_drawdown:.4f}，留出 {holdout.max_drawdown:.4f}，"
        f"全窗口 {full.max_drawdown:.4f}。"
    )
    return "\n".join(lines)


def run_stored_debate(
    *,
    symbol: str = "BTC-USDT",
    start: datetime,
    end: datetime,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    qty: Decimal = Decimal("0.01"),
    buying_power: Decimal = Decimal("1000000"),
    settings: Settings | None = None,
) -> tuple[DebateResult, int, int]:
    """从 ClickHouse ``market.bars`` 读 30m / 60m，再跑日线分配。"""
    from dataprice.market.repo import market_session
    from dataprice.strategy.candles import candles_from_frame

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
    result = run_debate(
        candles_30,
        candles_60,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        qty=qty,
        buying_power=buying_power,
        grid_params=GridParams(fee_bps=fee_bps, slippage_bps=slippage_bps),
    )
    return result, len(candles_30), len(candles_60)


def _first_index_at_or_after(candles: Sequence[Candle], ts: datetime) -> int | None:
    opens = [candle.open_ts for candle in candles]
    index = bisect_left(opens, ts)
    if index >= len(candles):
        return None
    return index


def _covered(candles: Sequence[Candle], decisions: Sequence[Decision]) -> tuple[bool, ...]:
    flags = [False] * len(decisions)
    ordered = sorted(range(len(decisions)), key=lambda index: decisions[index].close_ts)
    if not ordered:
        return ()
    cursor = 0
    for candle in candles:
        while (
            cursor + 1 < len(ordered)
            and decisions[ordered[cursor + 1]].close_ts <= candle.open_ts
        ):
            cursor += 1
        if decisions[ordered[cursor]].close_ts <= candle.open_ts:
            flags[ordered[cursor]] = True
    return tuple(flags)


def _in_window(ts: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and ts < start:
        return False
    if end is not None and ts >= end:
        return False
    return True


def _bucket(
    window: str,
    label: str,
    days: Sequence[Decision],
    trades: Sequence[ClosedTrade],
) -> Bucket:
    wins = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl < 0]
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
    ordered = sorted(trades, key=lambda trade: (trade.exit_ts, trade.entry_ts, trade.mode))
    for trade in ordered:
        equity += trade.pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
    count = len(trades)
    return Bucket(
        window=window,
        mode=label,
        days=len(days),
        trades=count,
        pnl=q_cash(sum((trade.pnl for trade in trades), Decimal("0"))),
        win_rate=(len(wins) / count) if count else 0.0,
        profit_factor=factor,
        max_drawdown=q_cash(max_dd),
    )


def _side_line(label: str, trades: Sequence[ClosedTrade]) -> str:
    parts: list[str] = []
    for mode in ("long", "short", "grid"):
        chosen = [trade for trade in trades if trade.mode == mode]
        if not chosen:
            parts.append(f"{mode} 无成交")
            continue
        buys = [trade for trade in chosen if trade.side == "buy"]
        sells = [trade for trade in chosen if trade.side == "sell"]
        buy_pnl = q_cash(sum((trade.pnl for trade in buys), Decimal("0")))
        sell_pnl = q_cash(sum((trade.pnl for trade in sells), Decimal("0")))
        parts.append(
            f"{mode} 多 {len(buys)}/{_signed(buy_pnl)}，空 {len(sells)}/{_signed(sell_pnl)}"
        )
    return f"方向（{label}）：" + "；".join(parts) + "。"


def _signed(value: Decimal) -> str:
    text = f"{value:.4f}"
    if value > 0:
        return "+" + text
    return text
