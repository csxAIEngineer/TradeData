"""用上一完整 UTC 日的 TPO 做结构，用当日 VWAP 收回做进场。

这不是 mikilab 开源包里的策略。那个包只有 TPO / Market Profile / VWAP 指标，
没有 ``strategy()``，也没有可核对的胜率。这里把拍卖理论收成一套机械规则：
只在 K 线收盘后下单，当天只用已经走完的上一日分布。

一根 K 线就是一个 TPO 时间块。锁死的 30 分钟书用 30 分钟；5 分钟和 10 分钟
只改块长和日终那一根（23:55、23:50），不改止损、价值区或振幅倍数。
价格行按该日高低自动分成大约 80 档，
原点在 0，档高是 tick 的整数倍。一根 K 线的最高价到最低价之间每一档加 1，
不看成交量。POC 取计数最高的档；并列时取档心离区间中点更近的，还并列就取更低的。
价值区从 POC 起，每次比较上下各 2 档的计数，大的一边纳入，相等就两边都纳入，
直到覆盖至少 70% 的 TPO。VAH / VAL 用价值区最外一档的边界，并夹在当日最高最低价里。
TPOC 用 POC 档的中心。

当日 VWAP 用 hlc3，按成交量加权，每个 UTC 日重新累计。标准差是成交量加权的总体标准差。
上一日收盘时的 VWAP 会记在分布上，当天不再改；v1 的进场不用这条阶梯线。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Sequence

from dataprice.config import Settings
from dataprice.contracts import Side, q_cash, q_px, q_qty
from dataprice.strategy.candles import Candle, assert_sorted
from dataprice.timeutil import UTC

HOLDOUT_START = datetime(2026, 3, 1, tzinfo=UTC)

ExitReason = Literal["stop", "target", "session", "end"]
Bias = Literal["bull", "bear", "inside"]


@dataclass(frozen=True)
class TpoVwapParams:
    """v1 的全部数字。``evaluated_params`` 返回这份默认值，回测不再搜索。"""

    tick: float = 0.1
    target_rows: int = 80
    row_limit: int = 400
    value_area: float = 0.70
    va_step: int = 2
    band_mult: float = 1.0
    range_mult: float = 1.5
    stop_buffer: float = 0.1
    qty: Decimal = Decimal("0.01")
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    hold_overnight: bool = False
    scale_at_band: bool = False
    scale_at_tpoc: bool = False
    band_scale: float = 0.5
    tpoc_scale: float = 0.5
    bar_minutes: int = 30

    def __post_init__(self) -> None:
        if self.tick <= 0:
            raise ValueError("tick 必须为正")
        if self.target_rows < 2 or self.row_limit < 2:
            raise ValueError("价格行数至少为 2")
        if not 0 < self.value_area <= 1:
            raise ValueError("value_area 必须在 (0, 1] 内")
        if self.va_step < 1:
            raise ValueError("va_step 必须 >= 1")
        if self.band_mult <= 0:
            raise ValueError("band_mult 必须为正")
        if self.range_mult <= 0:
            raise ValueError("range_mult 必须为正")
        if self.stop_buffer < 0:
            raise ValueError("stop_buffer 不能为负")
        if self.qty <= 0:
            raise ValueError("qty 必须为正")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("费用和滑点不能为负")
        if not 0 < self.band_scale < 1 or not 0 < self.tpoc_scale < 1:
            raise ValueError("减仓比例必须在 (0, 1) 内")
        if self.band_scale + self.tpoc_scale > 1:
            raise ValueError("两段减仓比例加起来不能超过 1")
        if self.bar_minutes < 1 or (24 * 60) % self.bar_minutes != 0:
            raise ValueError("bar_minutes 必须是能整除 1440 的正整数")


def evaluated_params() -> TpoVwapParams:
    """锁死的 v1。不要按训练或留出的成交改这些数。``bar_minutes`` 默认 30。"""
    return TpoVwapParams()


def session_last_clock(bar_minutes: int) -> tuple[int, int]:
    """UTC 日最后一根的开盘钟点。5 分钟是 23:55，10 分钟是 23:50，30 分钟是 23:30。"""
    if bar_minutes < 1 or (24 * 60) % bar_minutes != 0:
        raise ValueError("bar_minutes 必须是能整除 1440 的正整数")
    return divmod(24 * 60 - bar_minutes, 60)


TPO_FREQ_MINUTES = {"5m": 5, "10m": 10, "30m": 30}


@dataclass(frozen=True)
class DayProfile:
    """一个已经收完的 UTC 日。下一交易日才能用它。"""

    day: date
    high: float
    low: float
    tpoc: float
    vah: float
    val: float
    vwap: float | None
    upper: float | None
    lower: float | None
    step: float
    rows: int
    tpo_total: int
    va_tpo: int


@dataclass(frozen=True)
class TpoVwapTrade:
    side: Side
    entry_index: int
    exit_index: int
    entry_ts: datetime
    exit_ts: datetime
    entry: Decimal
    exit: Decimal
    qty: Decimal
    stop: float
    target: float
    reason: ExitReason
    pnl: Decimal
    r_multiple: float | None
    bias: Bias
    anchor_touch: bool


@dataclass(frozen=True)
class TpoVwapResult:
    trades: tuple[TpoVwapTrade, ...]
    bars: int
    first_open: datetime
    last_open: datetime
    first_close: float
    last_close: float
    sessions: int


@dataclass(frozen=True)
class WindowStats:
    name: str
    trades: int
    win_rate: float
    pnl: Decimal
    net_bps: float
    avg_r: float
    max_drawdown: Decimal
    profit_factor: float
    longs: int
    shorts: int
    long_pnl: Decimal
    short_pnl: Decimal


def auction_bias(day_open: float, profile: DayProfile) -> Bias:
    """开盘相对上一交易日价值区。等于 VAH 或 VAL 算价值区里面。"""
    if day_open > profile.vah:
        return "bull"
    if day_open < profile.val:
        return "bear"
    return "inside"


def anchored_vwap(
    bars: Sequence[Candle],
    *,
    band_mult: float = 1.0,
) -> tuple[float, float, float] | None:
    """日锚 VWAP 和 ±band 的标准差带。没有正成交量时返回 None。"""
    sum_pv = 0.0
    sum_v = 0.0
    sum_p2 = 0.0
    for bar in bars:
        if bar.volume <= 0:
            continue
        price = (bar.high + bar.low + bar.close) / 3.0
        sum_pv += price * bar.volume
        sum_v += bar.volume
        sum_p2 += price * price * bar.volume
    return _vwap_from_sums(sum_pv, sum_v, sum_p2, band_mult)


def tpo_profile(bars: Sequence[Candle], params: TpoVwapParams | None = None) -> DayProfile | None:
    """把一段已经结束的 K 线收成一个日分布。高低没有间隔时返回 None。"""
    params = params or evaluated_params()
    if not bars:
        return None
    low = min(bar.low for bar in bars)
    high = max(bar.high for bar in bars)
    low_tick = _to_ticks(low, params.tick)
    high_tick = _to_ticks(high, params.tick)
    if high_tick <= low_tick:
        return None
    step, first, last = _choose_step(low_tick, high_tick, params.target_rows, params.row_limit)
    counts = [0] * (last - first + 1)
    for bar in bars:
        lo = _to_ticks(bar.low, params.tick) // step
        hi = _to_ticks(bar.high, params.tick) // step
        lo = max(lo, first)
        hi = min(hi, last)
        for row in range(lo, hi + 1):
            counts[row - first] += 1
    total = sum(counts)
    if total <= 0:
        return None
    poc = _pick_poc(counts, first, step, low_tick, high_tick)
    va_lo, va_hi, va_tpo = _value_area(counts, poc - first, total, params.value_area, params.va_step)
    va_lo_row = first + va_lo
    va_hi_row = first + va_hi
    vah = min(high, (va_hi_row + 1) * step * params.tick)
    val = max(low, va_lo_row * step * params.tick)
    if vah <= val:
        return None
    tpoc = (poc + 0.5) * step * params.tick
    vwap_band = anchored_vwap(bars, band_mult=params.band_mult)
    vwap = upper = lower = None
    if vwap_band is not None:
        vwap, upper, lower = vwap_band
    return DayProfile(
        day=_utc_date(bars[0].open_ts),
        high=high,
        low=low,
        tpoc=tpoc,
        vah=vah,
        val=val,
        vwap=vwap,
        upper=upper,
        lower=lower,
        step=step * params.tick,
        rows=len(counts),
        tpo_total=total,
        va_tpo=va_tpo,
    )


def run_tpo_vwap(
    candles: Sequence[Candle],
    params: TpoVwapParams | None = None,
) -> TpoVwapResult:
    """跑 v1。同时只持有一笔，进场在信号那根的收盘价。块长看 ``params.bar_minutes``。"""
    params = params or evaluated_params()
    if not candles:
        raise ValueError("没有 K 线，不能做 TPO+VWAP")
    assert_sorted(candles)
    profiles = _completed_profiles(candles, params)
    trades = _simulate(candles, profiles, params)
    return TpoVwapResult(
        trades=tuple(trades),
        bars=len(candles),
        first_open=candles[0].open_ts,
        last_open=candles[-1].open_ts,
        first_close=candles[0].close,
        last_close=candles[-1].close,
        sessions=len(profiles),
    )


def run_stored_tpo_vwap(
    *,
    symbol: str = "BTC-USDT",
    start: datetime,
    end: datetime,
    params: TpoVwapParams | None = None,
    settings: Settings | None = None,
    freq: str = "30m",
) -> TpoVwapResult:
    """从 ClickHouse ``market.bars`` 读指定周期，再跑 TPO+VWAP。

    ``freq`` 会改写 ``bar_minutes``，所以 5 分钟不会在 23:30 被当成日终。
    """
    from dataprice.market.repo import market_session
    from dataprice.strategy.candles import candles_from_frame

    if freq not in TPO_FREQ_MINUTES:
        raise ValueError(f"TPO+VWAP 只读 5m、10m、30m，收到 {freq}")
    params = replace(params or evaluated_params(), bar_minutes=TPO_FREQ_MINUTES[freq])
    with market_session(settings) as repo:
        frame = repo.query_bars([symbol], freq, start, end)
    candles = tuple(candles_from_frame(frame))
    if not candles:
        if freq == "30m":
            hint = f"uv run dataprice backfill --symbol {symbol} --freq 30m"
        else:
            hint = "uv run dataprice binance-backfill --symbol BTCUSDT --months 36 --freq 5m"
        raise RuntimeError(f"ClickHouse 里没有 {symbol} {freq}。先执行: {hint}")
    return run_tpo_vwap(candles, params)


def window_stats(
    result: TpoVwapResult,
    name: str,
    start: datetime | None,
    end: datetime | None,
) -> WindowStats:
    """按进场 K 线的开盘时间切。``end`` 不含。"""
    chosen = [
        trade
        for trade in result.trades
        if (start is None or trade.entry_ts >= start) and (end is None or trade.entry_ts < end)
    ]
    wins = [trade for trade in chosen if trade.pnl > 0]
    losses = [trade.pnl for trade in chosen if trade.pnl < 0]
    gross_win = sum((trade.pnl for trade in wins), Decimal("0"))
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
    net_bps = 0.0
    r_total = 0.0
    r_count = 0
    long_pnl = Decimal("0")
    short_pnl = Decimal("0")
    longs = 0
    shorts = 0
    for trade in chosen:
        equity += trade.pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        notional = trade.entry * trade.qty
        if notional > 0:
            net_bps += float(trade.pnl / notional) * 10_000.0
        if trade.r_multiple is not None:
            r_total += trade.r_multiple
            r_count += 1
        if trade.side == "buy":
            longs += 1
            long_pnl += trade.pnl
        else:
            shorts += 1
            short_pnl += trade.pnl
    count = len(chosen)
    return WindowStats(
        name=name,
        trades=count,
        win_rate=(len(wins) / count) if count else 0.0,
        pnl=q_cash(sum((trade.pnl for trade in chosen), Decimal("0"))),
        net_bps=net_bps,
        avg_r=(r_total / r_count) if r_count else 0.0,
        max_drawdown=q_cash(max_dd),
        profit_factor=factor,
        longs=longs,
        shorts=shorts,
        long_pnl=q_cash(long_pnl),
        short_pnl=q_cash(short_pnl),
    )


def tpo_vwap_windows(result: TpoVwapResult) -> tuple[WindowStats, WindowStats, WindowStats]:
    return (
        window_stats(result, "训练", None, HOLDOUT_START),
        window_stats(result, "留出", HOLDOUT_START, None),
        window_stats(result, "全样本", None, None),
    )


def format_tpo_vwap_report(
    result: TpoVwapResult,
    params: TpoVwapParams | None = None,
) -> str:
    params = params or evaluated_params()
    reasons: dict[str, int] = {}
    touches = 0
    for trade in result.trades:
        reasons[trade.reason] = reasons.get(trade.reason, 0) + 1
        if trade.anchor_touch:
            touches += 1
    reason_text = "，".join(
        f"{name} {count}" for name, count in sorted(reasons.items(), key=lambda item: item[0])
    ) or "没有成交"
    lines = [
        "TPO+VWAP v1：上一完整 UTC 日的 TPO 价值区做结构，当日 hlc3 VWAP 收回做进场。",
        "mikilab 开源包只有指标，没有 strategy()，也没有可核对的 95% 胜率。下面的数字是这套规则自己的。",
        (
            f"费用每边 {params.fee_bps:g} bps，滑点每边 {params.slippage_bps:g} bps，"
            f"每手 {params.qty} BTC。往返 {(params.fee_bps + params.slippage_bps) * 2:g} bps。"
            "没有资金费。"
        ),
        (
            "进场在收盘价。多头要先交易到 max(昨日 VAL, 当日 VWAP) 再收在 VWAP 之上；"
            "空头镜像，交易到 min(昨日 VAH, 当日 VWAP) 再收在 VWAP 之下。"
            "开盘在价值区上方只做多，下方只做空，里面两个方向都允许，先出现的那个占用当天唯一的一笔。"
        ),
        (
            "止盈：进场还在 TPOC 盈利一侧时，整手在 TPOC 出；已经越过 TPOC，整手到对面 VA"
            "（多头 VAH，空头 VAL）。对面 VA 也不在盈利一侧就跳过。"
            f"止损在昨日极值之外 {params.stop_buffer:g} 倍价值区宽度。"
            f"当日振幅已经大于价值区宽度的 {params.range_mult:g} 倍就不再开仓。"
        ),
        (
            "默认当日收盘平仓，不持仓过夜。"
            f"±1σ 减仓 {'开' if params.scale_at_band else '关'}，"
            f"TPOC 先出 {params.tpoc_scale:g} 再留对面 VA {'开' if params.scale_at_tpoc else '关'}。"
        ),
        (
            f"K 线 {result.first_open:%Y-%m-%d %H:%M} → {result.last_open:%Y-%m-%d %H:%M}，"
            f"{params.bar_minutes}m {result.bars} 根，完整 TPO 日 {result.sessions}，平仓 {len(result.trades)}。"
            f"其中影线碰到 VAL/TPOC（空头则是 VAH/TPOC）的有 {touches} 笔。出场：{reason_text}。"
        ),
        "切分用进场 K 线的开盘时间。训练早于 2026-03-01，留出从这一天起。这些成交没有再拿去改参数。",
        "净基点是各笔盈亏除以该笔进场名义（进场价×手数）后的基点之和。平均 R 用成交价相对初始止损，不含手续费。",
        "最大回撤是该窗口已平仓盈亏按平仓顺序走出的峰值回撤，单位与净盈亏相同。",
        "",
        "| 窗口 | 笔数 | 胜率 | 净盈亏 | 净基点 | 平均R | 最大回撤 | 盈亏比 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    stats_rows = tpo_vwap_windows(result)
    for stats in stats_rows:
        lines.append(
            f"| {stats.name} | {stats.trades} | {stats.win_rate:.1%} | {_signed(stats.pnl)} | "
            f"{stats.net_bps:+.1f} | {stats.avg_r:+.2f} | {_signed(stats.max_drawdown)} | "
            f"{_pf(stats.profit_factor)} |"
        )
    lines.append("")
    for stats in stats_rows:
        lines.append(
            f"{stats.name} 多 {stats.longs} ({_signed(stats.long_pnl)})，"
            f"空 {stats.shorts} ({_signed(stats.short_pnl)})"
        )
    return "\n".join(lines)


@dataclass
class _Position:
    side: Side
    qty: Decimal
    left: Decimal
    entry: Decimal
    stop: float
    target: float
    runner: float | None
    entry_index: int
    entry_ts: datetime
    bias: Bias
    anchor_touch: bool
    first_target: float
    pnl_raw: Decimal
    blended_num: Decimal
    reason: ExitReason
    band_done: bool = False
    tpoc_done: bool = False


def _simulate(
    candles: Sequence[Candle],
    profiles: dict[date, DayProfile],
    params: TpoVwapParams,
) -> list[TpoVwapTrade]:
    qty = q_qty(params.qty)
    trades: list[TpoVwapTrade] = []
    position: _Position | None = None
    current: date | None = None
    profile: DayProfile | None = None
    day_open: float | None = None
    allow = False
    sum_pv = 0.0
    sum_v = 0.0
    sum_p2 = 0.0
    day_high = 0.0
    day_low = 0.0
    prev_upper: float | None = None
    prev_lower: float | None = None
    for index, bar in enumerate(candles):
        day = _utc_date(bar.open_ts)
        new_day = day != current
        if position is not None:
            closed = _advance(position, bar, index, candles, params, prev_upper, prev_lower)
            if closed is not None:
                trades.append(closed)
                position = None
        if new_day:
            current = day
            profile = profiles.get(day - timedelta(days=1))
            day_open = bar.open if _clock(bar.open_ts) == (0, 0) and profile is not None else None
            allow = day_open is not None
            sum_pv = 0.0
            sum_v = 0.0
            sum_p2 = 0.0
            day_high = bar.high
            day_low = bar.low
            prev_upper = None
            prev_lower = None
        else:
            day_high = max(day_high, bar.high)
            day_low = min(day_low, bar.low)
        band = _vwap_from_sums(sum_pv, sum_v, sum_p2, params.band_mult)
        if bar.volume > 0:
            typical = (bar.high + bar.low + bar.close) / 3.0
            sum_pv += typical * bar.volume
            sum_v += bar.volume
            sum_p2 += typical * typical * bar.volume
            band = _vwap_from_sums(sum_pv, sum_v, sum_p2, params.band_mult)
        if position is None and allow and profile is not None and day_open is not None and band is not None:
            if day_high - day_low > params.range_mult * (profile.vah - profile.val):
                allow = False
            elif _can_open(index, candles):
                opened = _try_open(
                    bar,
                    index,
                    profile,
                    day_open,
                    band[0],
                    params,
                    qty,
                )
                if opened is not None:
                    position = opened
                    allow = False
        if band is not None:
            prev_upper = band[1]
            prev_lower = band[2]
    return trades


def _advance(
    position: _Position,
    bar: Candle,
    index: int,
    candles: Sequence[Candle],
    params: TpoVwapParams,
    prev_upper: float | None,
    prev_lower: float | None,
) -> TpoVwapTrade | None:
    if index <= position.entry_index:
        return None
    if _stopped(position, bar):
        price = bar.open if _gapped_through_stop(position, bar) else position.stop
        _book(position, position.left, price, "stop", params)
        return _finish(position, index, bar.open_ts)
    _scale_band(position, bar, params, prev_upper, prev_lower)
    _take_structural(position, bar, params)
    if position.left > 0:
        reason = _flat_reason(index, candles, params)
        if reason is not None:
            _book(position, position.left, bar.close, reason, params)
    if position.left > 0:
        return None
    return _finish(position, index, bar.open_ts)


def _stopped(position: _Position, bar: Candle) -> bool:
    if position.side == "buy":
        return bar.open <= position.stop or bar.low <= position.stop
    return bar.open >= position.stop or bar.high >= position.stop


def _gapped_through_stop(position: _Position, bar: Candle) -> bool:
    if position.side == "buy":
        return bar.open <= position.stop
    return bar.open >= position.stop


def _scale_band(
    position: _Position,
    bar: Candle,
    params: TpoVwapParams,
    prev_upper: float | None,
    prev_lower: float | None,
) -> None:
    if not params.scale_at_band or position.band_done or position.left <= 0:
        return
    entry = float(position.entry)
    if position.side == "buy":
        if prev_upper is None or not entry < prev_upper < position.target or bar.high < prev_upper:
            return
        level = prev_upper
    else:
        if prev_lower is None or not position.target < prev_lower < entry or bar.low > prev_lower:
            return
        level = prev_lower
    part = _fraction(position.qty, params.band_scale, position.left)
    if part is None:
        return
    _book(position, part, level, "target", params)
    position.band_done = True


def _take_structural(position: _Position, bar: Candle, params: TpoVwapParams) -> None:
    if position.left <= 0:
        return
    if position.side == "buy" and bar.high < position.target:
        return
    if position.side == "sell" and bar.low > position.target:
        return
    runner_ready = (
        params.scale_at_tpoc
        and position.runner is not None
        and not position.tpoc_done
        and (
            (position.side == "buy" and position.runner > position.target)
            or (position.side == "sell" and position.runner < position.target)
        )
    )
    if runner_ready:
        part = _fraction(position.qty, params.tpoc_scale, position.left)
        if part is not None and part < position.left:
            runner = position.runner
            assert runner is not None
            _book(position, part, position.target, "target", params)
            position.target = runner
            position.runner = None
            position.tpoc_done = True
        else:
            _book(position, position.left, position.target, "target", params)
            return
    if position.left <= 0:
        return
    hit = bar.high >= position.target if position.side == "buy" else bar.low <= position.target
    if hit:
        _book(position, position.left, position.target, "target", params)


def _try_open(
    bar: Candle,
    index: int,
    profile: DayProfile,
    day_open: float,
    vwap: float,
    params: TpoVwapParams,
    qty: Decimal,
) -> _Position | None:
    bias = auction_bias(day_open, profile)
    side = _reclaim_side(bar, profile, vwap, bias)
    if side is None:
        return None
    entry = _fill(bar.close, side, params.slippage_bps)
    target, runner = _targets(side, float(entry), profile)
    if target is None:
        return None
    stop = _stop(side, profile, params)
    if side == "buy" and not stop < float(entry) < target:
        return None
    if side == "sell" and not target < float(entry) < stop:
        return None
    return _Position(
        side=side,
        qty=qty,
        left=qty,
        entry=entry,
        stop=stop,
        target=target,
        runner=runner,
        entry_index=index,
        entry_ts=bar.open_ts,
        bias=bias,
        anchor_touch=_anchor_touch(side, bar, profile),
        first_target=target,
        pnl_raw=Decimal("0"),
        blended_num=Decimal("0"),
        reason="end",
    )


def _reclaim_side(bar: Candle, profile: DayProfile, vwap: float, bias: Bias) -> Side | None:
    long_ok = bias in ("bull", "inside")
    short_ok = bias in ("bear", "inside")
    if long_ok and bar.low <= max(profile.val, vwap) and bar.close > vwap:
        return "buy"
    if short_ok and bar.high >= min(profile.vah, vwap) and bar.close < vwap:
        return "sell"
    return None


def _targets(side: Side, fill: float, profile: DayProfile) -> tuple[float | None, float | None]:
    if side == "buy":
        if fill < profile.tpoc:
            return profile.tpoc, profile.vah
        if fill < profile.vah:
            return profile.vah, None
        return None, None
    if fill > profile.tpoc:
        return profile.tpoc, profile.val
    if fill > profile.val:
        return profile.val, None
    return None, None


def _stop(side: Side, profile: DayProfile, params: TpoVwapParams) -> float:
    width = profile.vah - profile.val
    if side == "buy":
        return profile.low - params.stop_buffer * width
    return profile.high + params.stop_buffer * width


def _anchor_touch(side: Side, bar: Candle, profile: DayProfile) -> bool:
    """手册里的偏好：影线碰到 VAL/TPOC 或 VAH/TPOC。v1 只记录，不作为过滤。"""
    if side == "buy":
        return bar.low <= profile.val or bar.low <= profile.tpoc
    return bar.high >= profile.vah or bar.high >= profile.tpoc


def _book(
    position: _Position,
    slice_qty: Decimal,
    raw_price: float,
    reason: ExitReason,
    params: TpoVwapParams,
) -> None:
    close_side: Side = "sell" if position.side == "buy" else "buy"
    exit_px = _fill(raw_price, close_side, params.slippage_bps)
    entry_fee = _fee(slice_qty, position.entry, params.fee_bps)
    exit_fee = _fee(slice_qty, exit_px, params.fee_bps)
    if position.side == "buy":
        gross = slice_qty * (exit_px - position.entry)
    else:
        gross = slice_qty * (position.entry - exit_px)
    position.pnl_raw += gross - entry_fee - exit_fee
    position.blended_num += slice_qty * exit_px
    position.left -= slice_qty
    if position.left <= Decimal("0.0000001"):
        position.left = Decimal("0")
    position.reason = reason


def _finish(position: _Position, index: int, exit_ts: datetime) -> TpoVwapTrade:
    blended = position.blended_num / position.qty
    return TpoVwapTrade(
        side=position.side,
        entry_index=position.entry_index,
        exit_index=index,
        entry_ts=position.entry_ts,
        exit_ts=exit_ts,
        entry=position.entry,
        exit=q_px(blended),
        qty=position.qty,
        stop=position.stop,
        target=position.first_target,
        reason=position.reason,
        pnl=q_cash(position.pnl_raw),
        r_multiple=_r_multiple(position.side, position.entry, blended, position.stop),
        bias=position.bias,
        anchor_touch=position.anchor_touch,
    )


def _completed_profiles(
    candles: Sequence[Candle],
    params: TpoVwapParams,
) -> dict[date, DayProfile]:
    grouped: dict[date, list[Candle]] = {}
    for bar in candles:
        grouped.setdefault(_utc_date(bar.open_ts), []).append(bar)
    profiles: dict[date, DayProfile] = {}
    for day, bars in grouped.items():
        if not _session_complete(bars, params.bar_minutes):
            continue
        profile = tpo_profile(bars, params)
        if profile is not None:
            profiles[day] = profile
    return profiles


def _session_complete(bars: Sequence[Candle], bar_minutes: int) -> bool:
    clocks = {_clock(bar.open_ts) for bar in bars}
    return (0, 0) in clocks and session_last_clock(bar_minutes) in clocks


def _choose_step(low_tick: int, high_tick: int, target_rows: int, row_limit: int) -> tuple[int, int, int]:
    span = high_tick - low_tick
    step = max(1, (span + target_rows - 1) // target_rows)
    limit = min(target_rows, row_limit)
    first, last, rows = _row_span(low_tick, high_tick, step)
    while rows > limit:
        step += 1
        first, last, rows = _row_span(low_tick, high_tick, step)
    return step, first, last


def _row_span(low_tick: int, high_tick: int, step: int) -> tuple[int, int, int]:
    first = low_tick // step
    last = high_tick // step
    return first, last, last - first + 1


def _pick_poc(counts: Sequence[int], first: int, step: int, low_tick: int, high_tick: int) -> int:
    mid = (low_tick + high_tick) / 2.0
    best_key: tuple[int, float, float] | None = None
    best_row = first
    for offset, count in enumerate(counts):
        if count <= 0:
            continue
        row = first + offset
        center = (row + 0.5) * step
        key = (-count, abs(center - mid), center)
        if best_key is None or key < best_key:
            best_key = key
            best_row = row
    return best_row


def _value_area(
    counts: Sequence[int],
    poc_index: int,
    total: int,
    fraction: float,
    step: int,
) -> tuple[int, int, int]:
    lo = hi = poc_index
    included = counts[poc_index]
    target = fraction * total
    guard = 0
    while included < target and guard <= len(counts):
        guard += 1
        up: list[int] = []
        down: list[int] = []
        for offset in range(1, step + 1):
            above = hi + offset
            below = lo - offset
            if above < len(counts):
                up.append(above)
            if below >= 0:
                down.append(below)
        if not up and not down:
            break
        up_sum = sum(counts[index] for index in up)
        down_sum = sum(counts[index] for index in down)
        if up and down and up_sum == down_sum:
            chosen = up + down
            hi = up[-1]
            lo = down[-1]
        elif up and (not down or up_sum > down_sum):
            chosen = up
            hi = up[-1]
        elif down:
            chosen = down
            lo = down[-1]
        else:
            break
        included += sum(counts[index] for index in chosen)
    return lo, hi, included


def _vwap_from_sums(
    sum_pv: float,
    sum_v: float,
    sum_p2: float,
    band_mult: float,
) -> tuple[float, float, float] | None:
    if sum_v <= 0:
        return None
    mean = sum_pv / sum_v
    var = sum_p2 / sum_v - mean * mean
    if var < 0:
        var = 0.0
    stdev = math.sqrt(var)
    return mean, mean + band_mult * stdev, mean - band_mult * stdev


def _fraction(original: Decimal, fraction: float, left: Decimal) -> Decimal | None:
    part = q_qty(original * Decimal(str(fraction)))
    if part <= 0 or part > left:
        return None
    return part


def _flat_reason(
    index: int,
    candles: Sequence[Candle],
    params: TpoVwapParams,
) -> ExitReason | None:
    last = index + 1 >= len(candles)
    boundary = not last and _utc_date(candles[index + 1].open_ts) != _utc_date(candles[index].open_ts)
    clock_end = _clock(candles[index].open_ts) == session_last_clock(params.bar_minutes)
    if not params.hold_overnight and (boundary or clock_end):
        return "session"
    if last:
        return "end"
    return None


def _can_open(index: int, candles: Sequence[Candle]) -> bool:
    if index + 1 >= len(candles):
        return False
    return _utc_date(candles[index + 1].open_ts) == _utc_date(candles[index].open_ts)


def _r_multiple(side: Side, entry: Decimal, blended: Decimal, stop: float) -> float | None:
    entry_f = float(entry)
    exit_f = float(blended)
    risk = (entry_f - stop) if side == "buy" else (stop - entry_f)
    if risk <= 0:
        return None
    move = (exit_f - entry_f) if side == "buy" else (entry_f - exit_f)
    return move / risk


def _fill(price: float, side: Side, slippage_bps: float) -> Decimal:
    return _px(_slipped(price, side, slippage_bps))


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


def _signed(value: Decimal) -> str:
    text = f"{value:.4f}"
    if value > 0:
        return "+" + text
    return text


def _pf(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def _utc_date(ts: datetime) -> date:
    return ts.astimezone(UTC).date()


def _clock(ts: datetime) -> tuple[int, int]:
    utc = ts.astimezone(UTC)
    return utc.hour, utc.minute


def _to_ticks(price: float, tick: float) -> int:
    return int(round(price / tick))
