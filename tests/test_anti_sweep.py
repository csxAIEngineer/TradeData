"""扫流动性之后顺着扫的方向做。不连接数据库。"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from dataprice.strategy.backtest import (
    AntiSweepResult,
    BacktestResult,
    Trade,
    continuation_params_of_long,
    format_anti_sweep_report,
    simulate,
)
from dataprice.strategy.candles import Candle
from dataprice.strategy.imbalance import find_zones
from dataprice.strategy.liquidity import Level, LiquidityMap, Sweep
from dataprice.strategy.signals import (
    Signal,
    SignalBook,
    StrategyParams,
    continuation_params,
    entry_signals,
    evaluated_params,
    flip_to_continuation,
    long_params,
)
from dataprice.timeutil import UTC

T0 = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)


def test_evaluated_params_stay_the_fade_book():
    fade = evaluated_params()
    assert fade == StrategyParams(
        confirm_4h=True,
        max_risk_bps=80,
        liquidity_kinds=("eqh", "eql"),
        exit_on_bias_flip=False,
        entry="zone",
    )
    assert fade.continuation is False
    assert long_params().continuation is False
    assert continuation_params() == replace(fade, continuation=True)
    assert continuation_params_of_long() == replace(long_params(), continuation=True)
    assert evaluated_params() == fade


def test_flip_sends_equal_lows_short_and_equal_highs_long():
    faded_low = _signal(side="buy", entry=104, stop=97, target=112, liquidity="eql")
    continued = flip_to_continuation(faded_low)
    assert continued.side == "sell"
    assert continued.entry == 104
    assert continued.stop == 112
    assert continued.target == 97
    assert continued.liquidity == "eql"
    assert continued.index == faded_low.index
    assert continued.reason == "bear_fvg_after_eql"

    faded_high = _signal(side="sell", entry=100, stop=108, target=90, liquidity="eqh")
    bought = flip_to_continuation(faded_high)
    assert bought.side == "buy"
    assert bought.stop == 90
    assert bought.target == 108
    assert bought.liquidity == "eqh"
    assert bought.reason == "bull_fvg_after_eqh"


def test_flip_rejects_a_stop_on_the_wrong_side():
    with pytest.raises(ValueError, match="止损"):
        flip_to_continuation(_signal(side="buy", entry=100, stop=110, target=120, liquidity="eql"))
    with pytest.raises(ValueError, match="止盈"):
        flip_to_continuation(_signal(side="sell", entry=100, stop=90, target=80, liquidity="eqh"))


def test_continuation_flips_the_equal_low_fade_and_keeps_the_fade_path():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    liquidity = LiquidityMap((sell_side, buy_side), (Sweep(5, "sell", sell_side, 97.0),))
    zones = find_zones(candles)
    fade = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
    )
    continued = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=StrategyParams(continuation=True),
    )
    assert len(fade) == 1
    assert fade[0].side == "buy"
    assert fade[0].entry == 104
    assert fade[0].stop == 97
    assert fade[0].target == 112
    assert continued == [flip_to_continuation(fade[0])]
    assert continued[0].side == "sell"
    assert continued[0].liquidity == "eql"

    blocked = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=StrategyParams(continuation=True, short_min_er=0.99),
        short_er_at=tuple(0.0 for _ in candles),
    )
    assert blocked == continued


def test_continuation_short_stops_where_the_fade_would_take_profit():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    fade = _signal(side="buy", entry=104, stop=97, target=112, liquidity="eql", index=10, open_ts=candles[10].open_ts)
    book = SignalBook(tuple(candles), (fade,), bull, bull)
    faded = simulate(candles, book, qty=Decimal("0.01"))
    assert faded.trades[0].reason == "target"
    assert faded.trades[0].pnl == Decimal("0.08")

    flipped = SignalBook(tuple(candles), (flip_to_continuation(fade),), bull, bull)
    continued = simulate(candles, flipped, qty=Decimal("0.01"))
    assert continued.trades[0].side == "sell"
    assert continued.trades[0].reason == "stop"
    assert continued.trades[0].exit == Decimal("112")
    assert continued.trades[0].pnl == Decimal("-0.08")
    assert continued.trades[0].liquidity == "eql"


def test_report_splits_2026_and_the_march_holdout_on_entry_time():
    fade = _result(
        _trade(datetime(2025, 12, 1, tzinfo=UTC), "1"),
        _trade(datetime(2026, 2, 1, tzinfo=UTC), "1"),
        _trade(datetime(2026, 3, 2, tzinfo=UTC), "-4"),
    )
    continuation = _result(
        _trade(datetime(2025, 12, 1, tzinfo=UTC), "-1", side="sell"),
        _trade(datetime(2026, 2, 1, tzinfo=UTC), "-1", side="sell"),
        _trade(datetime(2026, 3, 2, tzinfo=UTC), "2", side="sell"),
    )
    empty = _result()
    text = format_anti_sweep_report(
        AntiSweepResult(
            fade=fade,
            continuation=continuation,
            eql_fade=empty,
            eql_continuation=empty,
            eqh_fade=empty,
            eqh_continuation=empty,
            long_fade=empty,
            long_continuation=empty,
            first_open=datetime(2025, 1, 1, tzinfo=UTC),
            last_open=datetime(2026, 10, 1, tzinfo=UTC),
            first_close=100,
            last_close=110,
            bars_30=3,
            bars_60=2,
        ),
        fee_bps=5,
        slippage_bps=2,
    )
    assert "| 全样本 | 反转 | 3 |" in text
    assert "| 2026年前 | 反转 | 1 |" in text
    assert "| 训练 <2026-03-01 | 反转 | 2 |" in text
    assert "| 2026年起 | 反转 | 2 |" in text
    assert "| 留出 ≥2026-03-01 | 反转 | 1 |" in text
    assert "| 全样本 | 延续 | 3 |" in text
    assert "| 留出 ≥2026-03-01 | 延续 | 1 |" in text
    assert "2026-01-01 到 2026-03-01" in text
    assert "evaluated_params" in text


def _candles(rows: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(T0 + timedelta(minutes=30 * index), open_, high, low, close, 1.0)
        for index, (open_, high, low, close) in enumerate(rows)
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


def _signal(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    liquidity: str,
    index: int = 10,
    open_ts: datetime = T0,
) -> Signal:
    return Signal(
        index=index,
        open_ts=open_ts,
        side=side,  # type: ignore[arg-type]
        entry=entry,
        stop=stop,
        target=target,
        zone_kind="fvg",
        sweep_index=5,
        zone_index=8,
        liquidity=liquidity,
    )


def _trade(ts: datetime, pnl: str, side: str = "buy") -> Trade:
    return Trade(
        side=side,  # type: ignore[arg-type]
        entry_index=1,
        exit_index=2,
        entry_ts=ts,
        exit_ts=ts,
        entry=Decimal("100"),
        exit=Decimal("101"),
        stop=90.0 if side == "buy" else 110.0,
        target=120.0 if side == "buy" else 80.0,
        reason="target",
        pnl=Decimal(pnl),
        zone_kind="fvg",
        liquidity="eql",
    )


def _result(*trades: Trade) -> BacktestResult:
    return BacktestResult(
        trades=trades,
        signals=len(trades),
        starting_buying_power=Decimal("100"),
        ending_buying_power=Decimal("100"),
    )
