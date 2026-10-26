"""ICT + SMC 信号和回测。不连接数据库。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dataprice.strategy.backtest import format_report, performance, simulate
from dataprice.strategy.candles import Candle, aggregate_candles
from dataprice.strategy.imbalance import find_zones
from dataprice.strategy.liquidity import Level, LiquidityMap, Sweep, map_liquidity
from dataprice.strategy.signals import (
    Signal,
    SignalBook,
    StrategyParams,
    bias_asof,
    entry_signals,
    evaluated_params,
    generate_signals,
    to_factor_frame,
)
from dataprice.strategy.structure import Swing, structure_path

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)


def _candles(rows: list[tuple[float, float, float, float]], start: datetime = T0, minutes: int = 30):
    out = []
    for index, (open_, high, low, close) in enumerate(rows):
        out.append(
            Candle(
                start + timedelta(minutes=minutes * index),
                open_,
                high,
                low,
                close,
                1.0,
            )
        )
    return out


def _flat(n: int, price: float = 100.0, start: datetime = T0) -> list[Candle]:
    return _candles([(price, price + 1, price - 1, price)] * n, start=start)


def _structure_rows():
    return [
        (9, 10, 8, 9),
        (8, 11, 7, 8),
        (11, 12, 9, 11),
        (13, 14, 10, 13),
        (12, 13, 11, 12),
        (15, 16, 12, 15),
        (14, 15, 13, 14),
        (12, 14, 11, 12),
        (12.5, 13, 12, 12.5),
        (10.5, 13, 10, 10.5),
    ]


def _long_rows():
    return [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 97, 100.5),
        (100.5, 102, 100.2, 101.5),
        (101.5, 105, 101.4, 104.5),
        (104.5, 106, 103.2, 105.5),
        (105.5, 106, 105, 105.2),
        (105.2, 105.4, 103.0, 104.0),
        (104, 113, 103.5, 111),
    ]


def test_bos_then_choch_and_no_lookahead():
    candles = _candles(_structure_rows())
    path = structure_path(candles, wing=1)
    assert [(event.kind, event.bias) for event in path.events] == [
        ("bos", "bull"),
        ("choch", "bear"),
    ]
    assert path.bias[5] == "bull"
    assert path.bias[-1] == "bear"
    for size in range(1, len(candles) + 1):
        assert structure_path(candles[:size], wing=1).bias == path.bias[:size]


def test_bias_asof_waits_until_higher_timeframe_closes():
    higher = _candles([(1, 2, 1, 1.5)], start=datetime(2026, 1, 6, tzinfo=UTC), minutes=60)
    lower = _candles(
        [(1, 2, 1, 1), (1, 2, 1, 1)],
        start=datetime(2026, 1, 6, tzinfo=UTC),
    )
    assert bias_asof(higher, ("bull",), "60m", lower, "30m") == ("neutral", "bull")


def test_equal_lows_can_be_swept_and_distant_highs_are_not():
    candles = _flat(8)
    candles[7] = Candle(candles[7].open_ts, 101, 102, 99, 101, 1)
    swings = (
        Swing(1, "low", 100.0, 3),
        Swing(4, "low", 100.04, 6),
    )
    liquidity = map_liquidity(candles, swings)
    sweeps = [sweep for sweep in liquidity.sweeps if sweep.level.kind == "eql"]
    assert len(sweeps) == 1
    assert sweeps[0].index == 7
    assert sweeps[0].side == "sell"
    assert sweeps[0].extreme == 99
    assert sweeps[0].level.price == pytest.approx(100.02)

    far = map_liquidity(
        _flat(4),
        (Swing(0, "high", 100.0, 1), Swing(2, "high", 102.0, 3)),
    )
    assert [level.kind for level in far.levels if level.kind == "eqh"] == []


def test_session_low_and_prior_day_low():
    session = _candles(
        [(102, 105, 100, 102), (104, 106, 101, 104), (103, 104, 99, 103)],
        start=datetime(2026, 1, 6, 7, 30, tzinfo=UTC),
    )
    swept = [sweep for sweep in map_liquidity(session, ()).sweeps if sweep.level.kind == "session_low"]
    assert len(swept) == 1
    assert swept[0].index == 2
    assert swept[0].level.price == 100

    days = _candles(
        [(105, 110, 100, 105), (106, 108, 104, 106), (103, 107, 99, 103)],
        start=datetime(2026, 1, 5, 23, 30, tzinfo=UTC),
    )
    prior = [sweep for sweep in map_liquidity(days, ()).sweeps if sweep.level.kind == "pdl"]
    assert len(prior) == 1
    assert prior[0].index == 2
    assert prior[0].level.price == 100


def test_bullish_fvg_and_order_block():
    candles = _candles(
        [
            (10, 11, 9, 9.5),
            (10, 12, 9.8, 11.5),
            (11.5, 12, 10, 10.5),
            (10.5, 14, 10.4, 13.5),
            (13.5, 15, 12.5, 14.5),
        ]
    )
    zones = find_zones(candles, ob_lookback=5)
    fvg = [zone for zone in zones if zone.kind == "fvg" and zone.bias == "bull"]
    blocks = [zone for zone in zones if zone.kind == "ob" and zone.bias == "bull"]
    assert len(fvg) == 1
    assert fvg[0].index == 4
    assert fvg[0].bottom == 12
    assert fvg[0].top == 12.5
    assert len(blocks) == 1
    assert blocks[0].bottom == 10
    assert blocks[0].top == 12
    assert find_zones(_candles([(1, 2, 1, 1.5), (1.5, 2, 1.2, 1.8), (1.8, 2.2, 1.4, 2)])) == ()


def test_long_entry_needs_sweep_return_and_both_biases():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    sweep = Sweep(5, "sell", sell_side, 97.0)
    zones = find_zones(candles)
    liquidity = LiquidityMap((sell_side, buy_side), (sweep,))
    signals = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
    )
    assert len(signals) == 1
    signal = signals[0]
    assert signal.side == "buy"
    assert signal.index == 10
    assert signal.zone_kind == "fvg"
    assert signal.liquidity == "eql"
    assert signal.entry == 104
    assert signal.stop == 97
    assert signal.target == 112

    bear = tuple("bear" if index == 10 else "bull" for index in range(len(candles)))
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bear,
            liquidity=liquidity,
            zones=zones,
        )
        == []
    )
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=LiquidityMap((sell_side, buy_side), ()),
            zones=zones,
        )
        == []
    )
    buy_sweep = Sweep(5, "buy", buy_side, 113.0)
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=LiquidityMap((sell_side, buy_side), (buy_sweep,)),
            zones=zones,
        )
        == []
    )


def test_factor_frame_marks_signal_at_close():
    candles = _candles(_long_rows())
    signal = Signal(
        index=10,
        open_ts=candles[10].open_ts,
        side="buy",
        entry=104,
        stop=97,
        target=112,
        zone_kind="fvg",
        sweep_index=5,
        zone_index=8,
        liquidity="eql",
    )
    bull = tuple("bull" for _ in candles)
    frame = to_factor_frame(SignalBook(tuple(candles), (signal,), bull, bull))
    assert list(frame.columns) == ["bias_1h", "bias_30m", "signal"]
    assert frame.index[10] == candles[10].close_ts("30m")
    assert frame.iloc[10]["signal"] == 1
    assert frame.iloc[0]["signal"] == 0


def test_generate_signals_is_pure():
    candles = _candles(_structure_rows())
    higher = _candles(
        [(9, 16, 7, 12), (12, 16, 10, 14), (14, 16, 11, 13)],
        start=T0,
        minutes=60,
    )
    book = generate_signals(candles, higher, StrategyParams(wing=1))
    assert len(book.bias_30m) == len(candles)
    assert len(book.bias_1h) == len(candles)
    assert list(to_factor_frame(book).columns) == ["bias_1h", "bias_30m", "signal"]


def test_backtest_target_stop_priority_and_bias_flip():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    signal = Signal(
        index=10,
        open_ts=candles[10].open_ts,
        side="buy",
        entry=104,
        stop=97,
        target=112,
        zone_kind="fvg",
        sweep_index=5,
        zone_index=8,
        liquidity="eql",
    )
    book = SignalBook(tuple(candles), (signal,), bull, bull)
    result = simulate(candles, book, qty=Decimal("0.01"))
    assert len(result.trades) == 1
    assert result.trades[0].reason == "target"
    assert result.trades[0].exit == Decimal("112")
    assert result.realized_pnl == Decimal("0.08")
    assert result.ending_buying_power - result.starting_buying_power == Decimal("0.08")
    assert "平仓 1" in format_report(result)

    stopped = _candles([(100, 101, 99, 100), (104, 105, 103, 104), (100, 130, 90, 110)])
    stop_signal = Signal(
        index=1,
        open_ts=stopped[1].open_ts,
        side="buy",
        entry=104,
        stop=97,
        target=112,
        zone_kind="fvg",
        sweep_index=0,
        zone_index=1,
        liquidity="pdl",
    )
    stop_book = SignalBook(
        tuple(stopped),
        (stop_signal,),
        ("bull", "bull", "bull"),
        ("bull", "bull", "bull"),
    )
    stop_result = simulate(stopped, stop_book, qty=Decimal("0.01"))
    assert stop_result.trades[0].reason == "stop"
    assert stop_result.trades[0].exit == Decimal("97")
    assert stop_result.realized_pnl == Decimal("-0.07")

    flipped = _candles([(100, 101, 99, 100), (104, 105, 103, 104), (100.5, 101, 99, 100.5)])
    flip_book = SignalBook(
        tuple(flipped),
        (
            Signal(
                index=1,
                open_ts=flipped[1].open_ts,
                side="buy",
                entry=104,
                stop=97,
                target=112,
                zone_kind="ob",
                sweep_index=0,
                zone_index=1,
                liquidity="session_low",
            ),
        ),
        ("bull", "bull", "bear"),
        ("bull", "bull", "bear"),
    )
    flip = simulate(flipped, flip_book, qty=Decimal("0.01"))
    assert flip.trades[0].reason == "bias_flip"
    assert flip.trades[0].exit == Decimal("100.5")


def test_short_target_and_notional_limit():
    candles = _candles([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 89, 95)])
    signal = Signal(
        index=1,
        open_ts=candles[1].open_ts,
        side="sell",
        entry=100,
        stop=105,
        target=90,
        zone_kind="fvg",
        sweep_index=0,
        zone_index=1,
        liquidity="eqh",
    )
    book = SignalBook(
        tuple(candles),
        (signal,),
        ("bear", "bear", "bear"),
        ("bear", "bear", "bear"),
    )
    result = simulate(candles, book, qty=Decimal("0.01"))
    assert result.trades[0].reason == "target"
    assert result.trades[0].pnl == Decimal("0.1")
    assert result.open_side is None

    blocked = simulate(
        candles,
        book,
        qty=Decimal("1"),
        max_order_notional=Decimal("10"),
    )
    assert blocked.trades == ()


def test_quality_filters_drop_wide_or_chased_entries():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    sweep = Sweep(5, "sell", sell_side, 97.0)
    zones = find_zones(candles)
    liquidity = LiquidityMap((sell_side, buy_side), (sweep,))
    tight = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=StrategyParams(max_risk_bps=80),
    )
    assert tight == []
    limited = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=StrategyParams(entry="zone"),
    )
    assert len(limited) == 1
    assert limited[0].entry == pytest.approx(103.2)


def test_slippage_and_disabled_bias_flip_change_the_fill():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    signal = Signal(
        index=10,
        open_ts=candles[10].open_ts,
        side="buy",
        entry=104,
        stop=97,
        target=112,
        zone_kind="fvg",
        sweep_index=5,
        zone_index=8,
        liquidity="eql",
    )
    book = SignalBook(tuple(candles), (signal,), bull, bull)
    plain = simulate(candles, book, qty=Decimal("0.01"))
    slipped = simulate(candles, book, qty=Decimal("0.01"), slippage_bps=10)
    assert slipped.trades[0].pnl < plain.trades[0].pnl
    stats = performance(plain)
    assert stats.win_rate == 1
    assert stats.pnl == Decimal("0.08")
    assert stats.max_drawdown == 0
    assert stats.profit_factor == float("inf")

    flipped = _candles([(100, 101, 99, 100), (104, 105, 103, 104), (100.5, 101, 99, 100.5)])
    flip_book = SignalBook(
        tuple(flipped),
        (
            Signal(
                index=1,
                open_ts=flipped[1].open_ts,
                side="buy",
                entry=104,
                stop=97,
                target=112,
                zone_kind="ob",
                sweep_index=0,
                zone_index=1,
                liquidity="session_low",
            ),
        ),
        ("bull", "bull", "bear"),
        ("bull", "bull", "bear"),
    )
    held = simulate(flipped, flip_book, qty=Decimal("0.01"), exit_on_bias_flip=False)
    assert held.trades[0].reason == "end"


def test_aggregate_drops_an_incomplete_4h_bucket():
    start = datetime(2026, 1, 6, tzinfo=UTC)
    candles = _candles([(1, 2, 1, 1.5)] * 5, start=start, minutes=60)
    candles[1] = Candle(candles[1].open_ts, 1.5, 3, 1.4, 2.5, 1)
    bars = aggregate_candles(candles, "4h")
    assert len(bars) == 1
    assert bars[0].open_ts == start
    assert bars[0].open == 1
    assert bars[0].high == 3
    assert bars[0].low == 1
    assert bars[0].close == candles[3].close


def test_evaluated_profile_is_the_tighter_rule_set():
    params = evaluated_params()
    assert params.confirm_4h is True
    assert params.max_risk_bps == 80
    assert params.liquidity_kinds == ("eqh", "eql")
    assert params.entry == "zone"
    assert params.exit_on_bias_flip is False
