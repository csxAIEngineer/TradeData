"""日线分配。不连接数据库，也不改三条猎手的默认参数。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dataprice.cli import main
from dataprice.strategy.backtest import simulate
from dataprice.strategy.candles import Candle
from dataprice.strategy.debate import (
    ER_BARS,
    ER_THRESHOLD,
    ClosedTrade,
    DebateResult,
    Decision,
    debate_buckets,
    decisions_asof,
    format_debate_report,
    is_strong_range,
    modes_for,
    resolve,
    resolve_claims,
    run_debate,
    select_signals,
)
from dataprice.strategy.grid import GridParams, GridResult, simulate_grid
from dataprice.strategy.liquidity import Level
from dataprice.strategy.signals import (
    Signal,
    SignalBook,
    long_params,
    regime_params,
    short_regime_params,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("er", "bias", "strong", "mode"),
    [
        (0.25, "bull", False, "long"),
        (0.25, "bull", True, "long"),
        (1.0, "bear", True, "short"),
        (0.25, "neutral", True, "flat"),
        (0.25, None, False, "flat"),
        (None, "bull", True, "flat"),
        (0.249, "bull", True, "grid"),
        (0.249, "bear", False, "flat"),
        (0.0, "neutral", False, "flat"),
        (float("nan"), "bull", True, "flat"),
        (0.5, "up", False, "flat"),
    ],
)
def test_resolve_follows_the_locked_daily_rules(er, bias, strong, mode):
    assert resolve(er=er, bias_4h=bias, strong_range=strong) == mode


def test_simultaneous_claims_resolve_flat():
    assert resolve_claims(("long", "short")) == "flat"
    assert resolve_claims(("long", "grid")) == "flat"
    assert resolve_claims(("short", "flat")) == "flat"
    assert resolve_claims(("grid",)) == "grid"
    assert resolve_claims(()) == "flat"
    assert resolve_claims(("flat",)) == "flat"
    assert resolve_claims(("both",)) == "flat"


def test_locked_thresholds_match_the_three_hunters():
    assert ER_THRESHOLD == 0.25
    assert ER_BARS == 18
    assert regime_params().min_er == ER_THRESHOLD
    assert regime_params().er_bars == ER_BARS
    assert short_regime_params().short_min_er == ER_THRESHOLD
    assert short_regime_params().short_er_bars == ER_BARS
    assert short_regime_params().sides == ("buy", "sell")
    assert GridParams().max_er == ER_THRESHOLD
    assert GridParams().er_bars == ER_BARS
    assert GridParams().spacing_bps == 50
    assert long_params().long_min_sweep_clv == 0.7
    assert long_params().sides == ("buy",)
    assert evaluated_defaults_unchanged()


def evaluated_defaults_unchanged():
    assert long_params().min_er is None
    assert short_regime_params().long_min_sweep_clv is None
    assert GridParams().max_er == 0.25
    return True


def test_a_daily_close_assigns_only_the_following_segment():
    close = T0 + timedelta(days=1)
    later = T0 + timedelta(days=2)
    decisions = (
        Decision(close, "long", 0.4, "bull", False),
        Decision(later, "grid", 0.1, "neutral", True),
    )
    candles = [
        Candle(T0 + timedelta(minutes=30 * index), 100, 100, 100, 100)
        for index in range(48 * 3)
    ]
    modes = modes_for(candles, decisions)
    assert modes[0] == "flat"
    last_of_first_day = candles[47]
    assert last_of_first_day.open_ts == close - timedelta(minutes=30)
    assert modes[47] == "flat"
    assert modes[48] == "long"
    assert candles[48].open_ts == close
    assert modes[95] == "long"
    assert modes[96] == "grid"
    assert candles[96].open_ts == later


def test_strong_range_requires_a_confirmed_equal_pair_that_fits_one_step():
    params = GridParams(range_lookback=4, spacing_bps=50)
    wide = _candles([(105, 110, 100, 105)] * 4)
    fit = (
        Level("eql", "sell", 102.0, 1),
        Level("eqh", "buy", 108.0, 2),
    )
    assert is_strong_range(wide, fit, 4, params) is True
    tight = _candles([(100, 130, 90, 100)] * 4)
    crowded = (Level("eql", "sell", 99.9, 1), Level("eqh", "buy", 100.1, 2))
    assert is_strong_range(tight, crowded, 4, params) is False
    assert is_strong_range(wide, (), 4, params) is False


def test_flat_prices_do_not_invent_a_mode_before_the_sample_exists():
    candles_60 = [Candle(T0 + timedelta(hours=index), 100, 100, 100, 100) for index in range(24 * 5)]
    candles_30 = [
        Candle(T0 + timedelta(minutes=30 * index), 100, 100, 100, 100) for index in range(48 * 5)
    ]
    found = decisions_asof(candles_30, candles_60)
    assert len(found) == 5
    assert {item.mode for item in found} == {"flat"}
    assert found[0].er is None
    assert found[-1].er == pytest.approx(0)
    assert modes_for(candles_30, found)[0] == "flat"
    result = run_debate(candles_30, candles_60, fee_bps=5, slippage_bps=2)
    assert result.trades == ()


def test_same_bar_conflict_and_the_other_paths_signal_are_dropped():
    buy = _signal(0, "buy")
    other = _signal(0, "sell")
    later = _signal(1, "buy")
    assert select_signals((buy, other), (), ("long", "flat")) == ()
    assert select_signals((later,), (buy,), ("short", "long")) == (buy, later)
    assert select_signals((), (buy,), ("short",)) == (buy,)
    assert select_signals((buy,), (), ("short",)) == ()


def test_mode_change_flattens_the_open_position_at_the_next_open():
    start = datetime(2026, 1, 6, 8, tzinfo=UTC)
    candles = [
        Candle(start, 100, 101, 99, 100),
        Candle(start + timedelta(minutes=30), 100, 110, 99, 108),
    ]
    signal = _signal(0, "buy", open_ts=candles[0].open_ts, entry=100, stop=90, target=130)
    book = SignalBook(tuple(candles), (signal,), ("bull", "bull"), ("bull", "bull"))
    held = simulate(candles, book, fee_bps=0, slippage_bps=0, exit_on_bias_flip=False)
    flat = simulate(
        candles,
        book,
        fee_bps=0,
        slippage_bps=0,
        exit_on_bias_flip=False,
        force_flat=(False, True),
    )
    assert held.trades[0].reason == "end"
    assert float(held.trades[0].exit) == pytest.approx(108)
    assert flat.trades[0].reason == "mode"
    assert flat.trades[0].exit_index == 1
    assert float(flat.trades[0].exit) == pytest.approx(100)


def test_grid_mask_replaces_the_intraday_efficiency_gate():
    candles = _candles([(105, 110, 100, 105)] * 4)
    candles.extend(
        _candles(
            [
                (104.8, 104.9, 101.0, 104.7),
                (104.8, 105.2, 104.6, 105.0),
                (105.0, 105.2, 104.8, 105.0),
            ],
            start=candles[-1].open_ts + timedelta(minutes=30),
        )
    )
    params = GridParams(
        er_bars=2,
        range_lookback=4,
        spacing_bps=40,
        max_inventory_units=1,
        fee_bps=5,
        slippage_bps=2,
    )
    blocked = simulate_grid(candles, (0.1,) * len(candles), params, enabled=(False,) * len(candles))
    forced = simulate_grid(candles, (0.9,) * len(candles), params, enabled=(True,) * len(candles))
    assert blocked.trades == ()
    assert len(forced.trades) == 1
    assert forced.trades[0].side == "buy"
    assert forced.trades[0].reason == "target"


def test_buckets_split_on_entry_time_and_keep_modes_exclusive():
    trades = (
        ClosedTrade("grid", "sell", datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 1, 16, tzinfo=UTC), Decimal("1")),
        ClosedTrade("long", "buy", datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 2, tzinfo=UTC), Decimal("5")),
        ClosedTrade("long", "buy", datetime(2026, 3, 2, tzinfo=UTC), datetime(2026, 3, 3, tzinfo=UTC), Decimal("-2")),
    )
    decisions = (
        Decision(datetime(2026, 1, 15, tzinfo=UTC), "grid", 0.1, "neutral", True),
        Decision(datetime(2026, 2, 1, tzinfo=UTC), "long", 0.4, "bull", False),
        Decision(datetime(2026, 2, 2, tzinfo=UTC), "flat", 0.4, "neutral", False),
        Decision(datetime(2026, 3, 2, tzinfo=UTC), "long", 0.5, "bull", False),
    )
    result = DebateResult(
        decisions=decisions,
        modes=(),
        trades=trades,
        grid_result=GridResult(
            trades=(),
            path=(),
            chop_bars=0,
            range_bars=0,
            opens=0,
            buying_power=Decimal("1000000"),
        ),
        covered=(True, True, True, True),
    )
    rows = {(row.window, row.mode): row for row in debate_buckets(result)}
    assert rows[("训练", "合计")].days == 3
    assert rows[("训练", "合计")].trades == 2
    assert rows[("训练", "合计")].pnl == Decimal("6.0000")
    assert rows[("训练", "合计")].profit_factor == float("inf")
    assert rows[("训练", "long")].trades == 1
    assert rows[("训练", "grid")].pnl == Decimal("1.0000")
    assert rows[("训练", "short")].days == 0
    assert rows[("训练", "flat")].days == 1
    assert rows[("训练", "flat")].trades == 0
    assert rows[("留出", "合计")].trades == 1
    assert rows[("留出", "合计")].pnl == Decimal("-2.0000")
    assert rows[("留出", "合计")].max_drawdown == Decimal("2.0000")
    assert rows[("留出", "long")].days == 1
    assert rows[("全窗口", "合计")].trades == 3
    assert rows[("全窗口", "合计")].pnl == Decimal("4.0000")
    assert rows[("全窗口", "合计")].profit_factor == pytest.approx(3)
    assert rows[("全窗口", "合计")].max_drawdown == Decimal("2.0000")
    assert rows[("全窗口", "long")].profit_factor == pytest.approx(2.5)
    text = format_debate_report(result, fee_bps=5, slippage_bps=2)
    assert "| 训练 | 合计 | 3 | 2 | +6.0000 |" in text
    assert "| 留出 | long | 1 | 1 | -2.0000 |" in text
    assert "没有用留出样本改过" in text
    assert "方向（留出）：long 多 1/-2.0000，空 0/0.0000；short 无成交；grid 无成交。" in text


def test_debate_cli_is_exclusive(capsys):
    with pytest.raises(SystemExit):
        main(["backtest", "--debate-daily", "--long"])
    err = capsys.readouterr().err
    assert "只能选一个" in err
    with pytest.raises(SystemExit):
        main(["backtest", "--debate-daily", "--buy-only"])
    assert "buy-only" in capsys.readouterr().err


def _candles(rows, start=T0, minutes=30):
    return [
        Candle(start + timedelta(minutes=minutes * index), open_, high, low, close, 1.0)
        for index, (open_, high, low, close) in enumerate(rows)
    ]


def _signal(index, side, open_ts=T0, entry=100.0, stop=90.0, target=120.0):
    return Signal(
        index=index,
        open_ts=open_ts,
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        zone_kind="fvg",
        sweep_index=0,
        zone_index=1,
        liquidity="eql",
    )
