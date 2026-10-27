"""TPO 价值区加当日 VWAP 收回。不连接数据库。"""

import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from dataprice.cli import main
from dataprice.contracts import q_cash, q_px
from dataprice.strategy.candles import Candle
from dataprice.strategy.tpo_vwap import (
    TpoVwapParams,
    anchored_vwap,
    auction_bias,
    evaluated_params,
    format_tpo_vwap_report,
    run_tpo_vwap,
    session_last_clock,
    tpo_profile,
    tpo_vwap_windows,
)
from dataprice.timeutil import UTC

D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)
D3 = date(2026, 1, 7)
FLAT = (100.0, 102.0, 98.0, 100.0, 1.0)
QUIET = (100.0, 101.0, 99.0, 100.0, 1.0)
RECLAIM = (100.0, 101.0, 97.0, 100.5, 10.0)


def test_evaluated_params_stay_locked():
    locked = evaluated_params()
    assert locked == TpoVwapParams(
        tick=0.1,
        target_rows=80,
        row_limit=400,
        value_area=0.70,
        va_step=2,
        band_mult=1.0,
        range_mult=1.5,
        stop_buffer=0.1,
        qty=Decimal("0.01"),
        fee_bps=5.0,
        slippage_bps=2.0,
        hold_overnight=False,
        scale_at_band=False,
        scale_at_tpoc=False,
        band_scale=0.5,
        tpoc_scale=0.5,
        bar_minutes=30,
    )
    run_tpo_vwap(_session(D1) + _session(D2), locked)
    assert evaluated_params() == locked


def test_vwap_matches_the_manual_example_and_population_bands():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bars = [
        Candle(start, 100, 100, 100, 100, 10),
        Candle(start + timedelta(minutes=30), 110, 110, 110, 110, 30),
    ]
    vwap, upper, lower = anchored_vwap(bars)
    assert vwap == pytest.approx(107.5)
    assert upper - vwap == pytest.approx(math.sqrt(18.75))
    assert vwap - lower == pytest.approx(math.sqrt(18.75))


def test_balanced_day_poc_value_area_and_tie_break():
    profile = tpo_profile(_session(D1), _zero())
    assert profile is not None
    assert profile.rows == 5
    assert profile.step == pytest.approx(1)
    assert profile.tpoc == pytest.approx(99.5)
    assert profile.vah == pytest.approx(102)
    assert profile.val == pytest.approx(98)
    assert profile.high == pytest.approx(102)
    assert profile.low == pytest.approx(98)
    assert profile.va_tpo / profile.tpo_total >= 0.70
    assert profile.vwap == pytest.approx(100)

    bare = [
        Candle(datetime(2026, 1, 5, tzinfo=UTC) + timedelta(minutes=30 * i), 100, 100, 100, 100, 1)
        for i in range(3)
    ]
    assert tpo_profile(bare, _zero()) is None


def test_poc_tie_prefers_the_nearer_middle_then_the_lower_row():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bars: list[Candle] = []
    cursor = start
    for _ in range(5):
        bars.append(Candle(cursor, 0, 0, 0, 0, 1))
        cursor += timedelta(minutes=30)
        bars.append(Candle(cursor, 1, 1, 1, 1, 1))
        cursor += timedelta(minutes=30)
    bars.append(Candle(cursor, 1, 2, 0, 1, 1))
    profile = tpo_profile(bars, _zero())
    assert profile is not None
    assert profile.tpoc == pytest.approx(0.5)
    assert profile.va_tpo / profile.tpo_total >= 0.70


def test_dominant_poc_stops_the_value_area_at_that_row():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bars = [Candle(start, 5, 10, 0, 5, 1)]
    for i in range(30):
        bars.append(Candle(start + timedelta(minutes=30 * (i + 1)), 5, 5, 5, 5, 1))
    profile = tpo_profile(bars, _zero())
    assert profile is not None
    assert profile.tpo_total == 41
    assert profile.va_tpo == 31
    assert profile.tpoc == pytest.approx(5.5)
    assert profile.val == pytest.approx(5)
    assert profile.vah == pytest.approx(6)


def test_open_on_the_value_edge_is_inside():
    profile = tpo_profile(_session(D1), _zero())
    assert profile is not None
    assert auction_bias(profile.vah, profile) == "inside"
    assert auction_bias(profile.val, profile) == "inside"
    assert auction_bias(profile.vah + 0.1, profile) == "bull"
    assert auction_bias(profile.val - 0.1, profile) == "bear"


def test_close_at_vwap_does_not_enter_and_the_first_day_cannot_trade():
    result = run_tpo_vwap(_session(D1) + _session(D2), _zero())
    assert result.sessions == 2
    assert result.trades == ()


def test_long_reclaim_past_tpoc_exits_at_the_opposite_va():
    candles = _session(D1) + _session(D2, {0: RECLAIM})
    result = run_tpo_vwap(candles, _zero())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "buy"
    assert trade.bias == "inside"
    assert trade.entry_ts == datetime(2026, 1, 6, tzinfo=UTC)
    assert trade.reason == "target"
    assert trade.entry == Decimal("100.500000")
    assert trade.exit == Decimal("102.000000")
    assert trade.target == pytest.approx(102)
    assert trade.stop == pytest.approx(97.6)
    assert trade.pnl == Decimal("0.0150")
    assert trade.anchor_touch is True
    assert trade.r_multiple == pytest.approx((102 - 100.5) / (100.5 - 97.6))


def test_long_below_tpoc_exits_the_full_size_there():
    candles = _session(D1) + _session(D2, {0: (100.0, 100.0, 97.0, 99.2, 10.0)})
    result = run_tpo_vwap(candles, _zero())
    trade = result.trades[0]
    assert trade.side == "buy"
    assert trade.reason == "target"
    assert trade.target == pytest.approx(99.5)
    assert trade.exit == Decimal("99.500000")
    assert trade.pnl == Decimal("0.0030")


def test_tpoc_scale_keeps_half_for_the_opposite_va():
    params = _zero(scale_at_tpoc=True)
    candles = _session(D1) + _session(D2, {0: (100.0, 100.0, 97.0, 99.2, 10.0)})
    result = run_tpo_vwap(candles, params)
    trade = result.trades[0]
    assert trade.reason == "target"
    assert trade.pnl == Decimal("0.0155")
    assert trade.exit == Decimal("100.750000")


def test_band_scale_takes_half_before_the_structural_target():
    params = _zero(scale_at_band=True)
    overrides = {
        0: RECLAIM,
        1: (100.0, 101.5, 100.0, 101.5, 10.0),
        2: (100.0, 101.2, 100.0, 101.0, 1.0),
        3: (100.0, 102.0, 100.0, 102.0, 1.0),
    }
    overrides.update({i: QUIET for i in range(4, 48)})
    result = run_tpo_vwap(_session(D1) + _session(D2, overrides), params)
    assert len(result.trades) == 1
    assert result.trades[0].reason == "target"
    assert result.trades[0].pnl == Decimal("0.0100")


def test_stop_wins_when_the_same_bar_also_reaches_the_target():
    candles = _session(D1) + _session(D2, {0: RECLAIM, 1: (100.0, 103.0, 97.0, 100.0, 1.0)})
    trade = run_tpo_vwap(candles, _zero()).trades[0]
    assert trade.reason == "stop"
    assert trade.exit == Decimal("97.600000")
    assert trade.pnl == Decimal("-0.0290")


def test_one_entry_per_day_even_after_a_stop():
    overrides = {
        0: RECLAIM,
        1: (100.0, 103.0, 97.0, 100.0, 1.0),
        2: RECLAIM,
    }
    overrides.update({i: QUIET for i in range(3, 48)})
    result = run_tpo_vwap(_session(D1) + _session(D2, overrides), _zero())
    assert len(result.trades) == 1
    assert result.trades[0].reason == "stop"


def test_session_flat_does_not_hold_past_the_utc_day():
    overrides = {0: RECLAIM}
    overrides.update({i: QUIET for i in range(1, 48)})
    trade = run_tpo_vwap(_session(D1) + _session(D2, overrides), _zero()).trades[0]
    assert trade.reason == "session"
    assert trade.exit_ts.astimezone(UTC).date() == D2
    assert trade.entry_ts.astimezone(UTC).date() == D2
    assert trade.pnl == Decimal("-0.0050")


def test_overnight_flag_can_carry_into_the_next_session():
    quiet = {0: RECLAIM}
    quiet.update({i: QUIET for i in range(1, 48)})
    day3 = {0: QUIET, 1: (100.0, 102.0, 100.0, 101.0, 1.0)}
    day3.update({i: QUIET for i in range(2, 48)})
    candles = _session(D1) + _session(D2, quiet) + _session(D3, day3)
    carried = run_tpo_vwap(candles, _zero(hold_overnight=True)).trades[0]
    assert carried.reason == "target"
    assert carried.exit_ts.astimezone(UTC).date() == D3
    flat = run_tpo_vwap(candles, _zero()).trades[0]
    assert flat.reason == "session"
    assert flat.exit_ts.astimezone(UTC).date() == D2


def test_expansion_filter_blocks_the_day():
    wild = {0: (100.0, 110.0, 90.0, 101.0, 1.0)}
    blocked = run_tpo_vwap(_session(D1) + _session(D2, wild), _zero())
    assert blocked.trades == ()
    allowed = run_tpo_vwap(_session(D1) + _session(D2, wild), _zero(range_mult=100))
    assert len(allowed.trades) == 1
    assert allowed.trades[0].side == "buy"


def test_bullish_open_does_not_take_the_short_reclaim():
    candles = _session(D1) + _session(D2, {0: (103.0, 104.0, 100.0, 100.5, 10.0)})
    assert run_tpo_vwap(candles, _zero()).trades == ()


def test_bearish_open_shorts_the_failed_vwap_and_ignores_a_long_shape():
    candles = _session(D1) + _session(D2, {0: (97.0, 102.0, 97.0, 98.5, 10.0)})
    trade = run_tpo_vwap(candles, _zero()).trades[0]
    assert trade.side == "sell"
    assert trade.bias == "bear"
    assert trade.reason == "target"
    assert trade.exit == Decimal("98.000000")
    assert trade.pnl == Decimal("0.0050")
    assert trade.stop == pytest.approx(102.4)


def test_a_shallow_reclaim_is_kept_even_without_the_value_touch():
    candles = _session(D1) + _session(D2, {0: (100.5, 101.0, 100.2, 100.9, 1.0)})
    trade = run_tpo_vwap(candles, _zero()).trades[0]
    assert trade.side == "buy"
    assert trade.anchor_touch is False
    assert trade.target == pytest.approx(102)


def test_incomplete_prior_day_is_not_replaced_by_an_older_profile():
    day2 = _session(D2, n=24)
    day3 = _session(D3, {0: RECLAIM})
    result = run_tpo_vwap(_session(D1) + day2 + day3, _zero())
    assert D2 not in {trade.entry_ts.astimezone(UTC).date() for trade in result.trades}
    assert result.trades == ()


def test_no_entry_on_the_last_bar_of_the_session():
    overrides = {i: QUIET for i in range(48)}
    overrides[47] = RECLAIM
    assert run_tpo_vwap(_session(D1) + _session(D2, overrides), _zero()).trades == ()


def test_later_spike_does_not_change_the_prior_day_target():
    overrides = {0: RECLAIM, 5: (100.0, 500.0, 99.0, 200.0, 1.0)}
    overrides.update({i: QUIET for i in range(1, 48) if i != 5})
    trade = run_tpo_vwap(_session(D1) + _session(D2, overrides), _zero()).trades[0]
    assert trade.reason == "target"
    assert trade.exit == Decimal("102.000000")
    assert trade.stop == pytest.approx(97.6)
    prior = tpo_profile(_session(D1), _zero())
    assert prior is not None
    assert trade.target == pytest.approx(prior.vah)
    assert trade.stop == pytest.approx(prior.low - 0.1 * (prior.vah - prior.val))


def test_costs_slip_against_the_fill_and_charge_both_sides():
    candles = _session(D1) + _session(D2, {0: RECLAIM})
    trade = run_tpo_vwap(candles, TpoVwapParams(tick=1)).trades[0]
    entry = q_px(Decimal(format(100.5 * 1.0002, ".10f")))
    exit_ = q_px(Decimal(format(102 * 0.9998, ".10f")))
    qty = Decimal("0.01")
    fee_in = q_cash(qty * entry * Decimal("5") / Decimal("10000"))
    fee_out = q_cash(qty * exit_ * Decimal("5") / Decimal("10000"))
    assert trade.entry == entry
    assert trade.exit == exit_
    assert trade.pnl == q_cash(qty * (exit_ - entry) - fee_in - fee_out)
    assert trade.pnl < Decimal("0.0150")


def test_report_splits_train_and_holdout_on_entry_open_time():
    february = _session(date(2026, 2, 27)) + _session(date(2026, 2, 28), {0: RECLAIM})
    march = _session(date(2026, 3, 1), {0: RECLAIM})
    result = run_tpo_vwap(february + march, _zero())
    train, holdout, full = tpo_vwap_windows(result)
    assert train.trades == 1
    assert holdout.trades == 1
    assert full.trades == 2
    assert train.win_rate == 1
    assert holdout.pnl == train.pnl
    text = format_tpo_vwap_report(result, _zero())
    assert "训练" in text and "留出" in text and "全样本" in text
    assert "95%" in text


def test_cli_exposes_tpo_vwap_and_rejects_a_second_strategy(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "--help"])
    assert exc.value.code == 0
    assert "--tpo-vwap" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "--tpo-vwap", "--anti-sweep"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "--freq", "5m"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as help_exc:
        main(["--help"])
    assert help_exc.value.code == 0
    assert "binance-backfill" in capsys.readouterr().out


def test_session_clock_follows_bar_length():
    assert session_last_clock(30) == (23, 30)
    assert session_last_clock(10) == (23, 50)
    assert session_last_clock(5) == (23, 55)
    params = TpoVwapParams(tick=1, fee_bps=0, slippage_bps=0, bar_minutes=5)
    through_2330 = _span(D1, 5, 23 * 60 + 30)
    assert run_tpo_vwap(through_2330, params).sessions == 0
    assert run_tpo_vwap(through_2330, TpoVwapParams(tick=1, bar_minutes=30)).sessions == 1
    complete = _span(D1, 5, 23 * 60 + 55)
    assert run_tpo_vwap(complete, params).sessions == 1
    text = format_tpo_vwap_report(run_tpo_vwap(complete, params), params)
    assert "5m " in text


def _zero(**overrides: object) -> TpoVwapParams:
    params = TpoVwapParams(tick=1, fee_bps=0, slippage_bps=0)
    if not overrides:
        return params
    from dataclasses import replace

    return replace(params, **overrides)


def _span(day: date, step: int, last_minute: int) -> list[Candle]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    candles: list[Candle] = []
    minute = 0
    while minute <= last_minute:
        open_, high, low, close, volume = FLAT
        candles.append(Candle(start + timedelta(minutes=minute), open_, high, low, close, volume))
        minute += step
    return candles


def _session(
    day: date,
    overrides: dict[int, tuple[float, float, float, float, float]] | None = None,
    n: int = 48,
) -> list[Candle]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    overrides = overrides or {}
    candles: list[Candle] = []
    for index in range(n):
        open_, high, low, close, volume = overrides.get(index, FLAT)
        candles.append(Candle(start + timedelta(minutes=30 * index), open_, high, low, close, volume))
    return candles
