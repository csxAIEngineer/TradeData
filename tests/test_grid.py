"""震荡网格。不连接数据库。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dataprice.cli import main
from dataprice.contracts import q_cash, q_px
from dataprice.strategy.candles import Candle, aggregate_candles
from dataprice.strategy.grid import (
    GridParams,
    active_bracket,
    run_grid,
    simulate_grid,
)
from dataprice.strategy.liquidity import Level
from dataprice.strategy.regime import efficiency_asof
from dataprice.strategy.signals import StrategyParams, evaluated_params, regime_params

UTC = timezone.utc
T0 = datetime(2026, 1, 5, tzinfo=UTC)


def _candles(rows, start=T0, minutes=30):
    out = []
    for index, (open_, high, low, close) in enumerate(rows):
        out.append(Candle(start + timedelta(minutes=minutes * index), open_, high, low, close, 1.0))
    return out


def _params(**kwargs) -> GridParams:
    base = dict(
        er_bars=2,
        range_lookback=4,
        spacing_bps=40,
        max_inventory_units=1,
        range_break_bps=15,
        max_drawdown_bps=500,
        cooldown_bars=0,
        fee_bps=5,
        slippage_bps=2,
    )
    base.update(kwargs)
    return GridParams(**base)


def _flat_then(*extra):
    rows = [(105, 110, 100, 105)] * 4
    rows.extend(extra)
    return _candles(rows)


def _er(n, value=0.1):
    return tuple(value for _ in range(n))


def test_grid_defaults_follow_the_chop_side_of_the_regime_gate():
    params = GridParams()
    regime = regime_params()
    assert params.max_er == regime.min_er == 0.25
    assert params.er_bars == regime.er_bars == 18
    assert params.fee_bps == 5
    assert params.slippage_bps == 2
    assert params.eq_bps == 8
    assert params.eq_lookback == 48


def test_grid_does_not_change_the_trend_candidate():
    assert evaluated_params() == StrategyParams(
        confirm_4h=True,
        max_risk_bps=80,
        liquidity_kinds=("eqh", "eql"),
        exit_on_bias_flip=False,
        entry="zone",
    )
    assert evaluated_params().min_er is None


def test_grid_params_reject_a_non_positive_spacing():
    with pytest.raises(ValueError):
        GridParams(spacing_bps=0)
    with pytest.raises(ValueError):
        GridParams(max_er=0)


def test_target_round_trip_charges_fee_and_adverse_slippage():
    candles = _flat_then(
        (104.8, 104.9, 101.0, 104.7),
        (104.8, 105.2, 104.6, 105.0),
        (105.0, 105.2, 104.8, 105.0),
    )
    result = simulate_grid(candles, _er(len(candles)), _params())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "buy"
    assert trade.reason == "target"
    assert trade.range_kind == "range"
    assert trade.entry_index == 4
    assert trade.exit_index == 5
    assert float(trade.entry) == pytest.approx(104.58 * 1.0002, rel=1e-6)
    assert trade.exit == q_px(Decimal(format(105 * (1 - 2 / 10_000), ".10f")))
    qty = Decimal("0.01")
    fee_in = q_cash(qty * trade.entry * Decimal("5") / Decimal("10000"))
    fee_out = q_cash(qty * trade.exit * Decimal("5") / Decimal("10000"))
    gross = q_cash(qty * (trade.exit - trade.entry))
    assert trade.pnl == q_cash(gross - fee_in - fee_out)
    assert trade.pnl > 0
    assert max(mark.peak_units for mark in result.path) == 1


def test_a_bar_that_spans_entry_and_target_does_not_scalp():
    candles = _flat_then((104.0, 106.0, 101.0, 105.0), (105.0, 105.2, 104.8, 105.0))
    result = simulate_grid(candles, _er(len(candles)), _params())
    assert result.trades == ()
    assert result.opens == 0


def test_a_sweep_wick_through_the_edge_is_not_a_stop():
    candles = _flat_then(
        (104.8, 104.9, 99.0, 104.7),
        (104.8, 104.9, 104.6, 104.8),
        (104.8, 104.9, 104.6, 104.8),
    )
    result = simulate_grid(candles, _er(len(candles)), _params())
    assert result.opens == 1
    assert [trade.reason for trade in result.trades] == ["end"]


def test_a_close_through_the_range_stops_the_book():
    candles = _flat_then(
        (104.8, 104.9, 101.0, 104.7),
        (104.0, 104.5, 98.0, 99.0),
        (99.0, 99.5, 98.5, 99.2),
    )
    result = simulate_grid(candles, _er(len(candles)), _params())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == "range_break"
    assert trade.exit_index == 5
    assert trade.exit == q_px(Decimal(format(99 * (1 - 2 / 10_000), ".10f")))
    assert trade.pnl < 0


def test_inventory_cap_binds_as_price_walks_down_the_grid():
    rows = [(105, 110, 100, 105)] * 4
    rows.append((104.7, 104.9, 104.3, 104.6))
    rows.append((104.3, 104.4, 103.9, 104.2))
    rows.append((104.2, 104.3, 104.0, 104.2))
    candles = _candles(rows)
    held = {}
    for cap in (1, 2):
        result = simulate_grid(
            candles,
            _er(len(candles)),
            _params(max_inventory_units=cap, max_drawdown_bps=10_000),
        )
        held[cap] = max(mark.peak_units for mark in result.path)
        assert result.opens == cap
        assert held[cap] == cap
    assert held[2] > held[1]


def test_drawdown_halt_blocks_reentry_for_the_cooldown():
    rows = [(105, 110, 100, 105)] * 4
    rows.append((104.8, 104.9, 101.0, 104.7))
    rows.append((101.5, 102.0, 100.5, 101.0))
    dip = (104.8, 104.9, 101.0, 104.7)
    wide = (105.0, 110.0, 100.0, 105.0)
    rows.extend([dip, dip, wide, dip])
    rows.append((104.8, 104.9, 104.6, 104.8))
    candles = _candles(rows)
    result = simulate_grid(
        candles,
        _er(len(candles)),
        _params(max_drawdown_bps=50, cooldown_bars=3),
    )
    assert result.trades[0].reason == "drawdown"
    assert result.trades[0].exit_index == 5
    entries = {trade.entry_index for trade in result.trades}
    assert 4 in entries
    assert entries.isdisjoint({6, 7, 8})
    assert 9 in entries


def test_current_bar_efficiency_does_not_open_the_grid():
    candles = _flat_then((104.8, 104.9, 101.0, 104.7), (105.0, 105.1, 104.9, 105.0))
    er = [0.9, 0.9, 0.9, 0.9, 0.0, 0.9]
    result = simulate_grid(candles, er, _params())
    assert result.trades == ()


def test_a_rising_efficiency_flattens_at_the_next_open():
    candles = _flat_then((104.8, 104.9, 101.0, 104.7), (104.8, 110.0, 104.6, 109.0))
    er = [0.1] * 4 + [0.9, 0.9]
    result = simulate_grid(candles, er, _params())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == "trend"
    assert trade.exit_index == 5
    assert float(trade.exit) == pytest.approx(104.8 * (1 - 2 / 10_000), rel=1e-6)
    assert float(trade.exit) < 105


def test_equal_high_and_low_outrank_the_rolling_edges():
    candles = _flat_then()
    levels = (
        Level("eql", "sell", 102.0, 1),
        Level("eqh", "buy", 108.0, 2),
        Level("eqh", "buy", 140.0, 4),
    )
    params = _params(spacing_bps=40)
    bracket = active_bracket(candles, levels, 4, params)
    assert bracket is not None
    assert bracket.kind == "eq"
    assert bracket.lower == 102
    assert bracket.upper == 108

    stale = (Level("eql", "sell", 102.0, 4), Level("eqh", "buy", 108.0, 4))
    rolled = active_bracket(candles, stale, 4, params)
    assert rolled is not None
    assert rolled.kind == "range"
    assert rolled.lower == 100
    assert rolled.upper == 110


def test_a_tight_equal_pair_falls_back_to_the_rolling_range():
    candles = _candles([(100, 130, 90, 100)] * 4)
    levels = (Level("eql", "sell", 99.9, 1), Level("eqh", "buy", 100.1, 2))
    bracket = active_bracket(candles, levels, 4, _params(spacing_bps=50))
    assert bracket is not None
    assert bracket.kind == "range"
    assert bracket.lower == 90
    assert bracket.upper == 130


def test_run_grid_trades_the_dip_only_while_the_4h_path_is_choppy():
    thirty = _session_30m()
    chop = _session_60m({3: 110, 7: 100, 11: 110})
    trend = _session_60m({3: 100, 7: 110, 11: 120})
    er = efficiency_asof(aggregate_candles(chop, "4h"), "4h", thirty, "30m", 2)
    assert er[24] == pytest.approx(0)
    bought = run_grid(thirty, chop, _params())
    assert len(bought.trades) == 1
    assert bought.trades[0].side == "buy"
    assert bought.trades[0].reason == "target"
    assert efficiency_asof(aggregate_candles(trend, "4h"), "4h", thirty, "30m", 2)[24] == pytest.approx(1)
    assert run_grid(thirty, trend, _params()).trades == ()


def test_grid_cli_is_exclusive_with_the_trend_profiles(capsys):
    with pytest.raises(SystemExit):
        main(["backtest", "--grid", "--regime"])
    err = capsys.readouterr().err
    assert "只能选一个" in err
    assert "unrecognized" not in err


def _session_30m():
    rows = [(105, 105, 105, 105)] * 21
    rows.extend([(105, 110, 100, 105)] * 4)
    rows.extend(
        [
            (104.8, 104.9, 101.0, 104.7),
            (104.8, 105.2, 104.6, 105.0),
            (105.0, 105.2, 104.8, 105.0),
        ]
    )
    return _candles(rows)


def _session_60m(closes):
    out = []
    price = 100.0
    for hour in range(12):
        price = closes.get(hour, price)
        out.append(Candle(T0 + timedelta(hours=hour), price, price, price, price, 1.0))
    return out
