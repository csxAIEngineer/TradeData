"""一击必杀 v0。不连接数据库。"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from dataprice.cli import main
from dataprice.contracts import q_cash
from dataprice.strategy.candles import Candle
from dataprice.strategy.sniper.backtest import run_sniper, run_sniper_intraday
from dataprice.strategy.sniper.bias import daily_bias, ema_close
from dataprice.strategy.sniper.entry import entry_series, find_ema_turn, inclusive_percentile, low_atr_ratio, stop_beyond_wick
from dataprice.strategy.sniper.fetch import parse_fred_csv, parse_yahoo_chart
from dataprice.strategy.sniper.macro_gate import (
    GateCalendar,
    MacroBook,
    MacroPoint,
    parse_macro_csv,
    risk_on_component,
)
from dataprice.strategy.sniper.params import SniperParams, evaluated_params
from dataprice.strategy.sniper.report import format_sniper_report
from dataprice.strategy.sniper.resample import to_4h, to_daily
from dataprice.strategy.sniper.risk import plan_trade
from dataprice.strategy.sniper.store import book_from_frame, factor_frame, gates_from_frame
from dataprice.timeutil import UTC

MON = date(2026, 1, 5)


def test_evaluated_params_stay_locked():
    locked = evaluated_params()
    assert locked == SniperParams()
    assert locked.theta == 0.55
    assert locked.macro_version == "v0.1"
    assert locked.macro_lookback == 63
    assert locked.ema_span == 50
    assert locked.structure_days == 20
    assert locked.pivot_k == 2
    assert locked.ema_fast == 6
    assert locked.ema_mid == 12
    assert locked.ema_slow == 32
    assert locked.ema_slope_bars == 1
    assert locked.atr_length == 14
    assert locked.atr_percentile_window == 20
    assert locked.atr_percentile == 0.30
    assert not hasattr(locked, "atr_median_window")
    assert locked.min_account_rr == 3
    assert locked.leverage == 20
    assert not hasattr(locked, "risk_fraction")
    assert locked.equity == Decimal("10000")
    assert SniperParams(min_account_rr=1.618).min_account_rr == 1.618
    assert SniperParams(min_account_rr=1).min_account_rr == 1
    assert SniperParams(min_account_rr=10).min_account_rr == 10
    with pytest.raises(ValueError):
        SniperParams(min_account_rr=0.99)
    with pytest.raises(ValueError):
        SniperParams(atr_percentile=0)
    with pytest.raises(ValueError):
        SniperParams(macro_version="v0.2")


def test_component_is_one_at_or_below_the_median():
    assert risk_on_component([1, 2, 3]) == 0
    assert risk_on_component([3, 2, 1]) == 1
    assert risk_on_component([1, 3, 2]) == 1


def test_theta_055_needs_three_of_four_factors():
    params = _params()
    monday = date(2026, 1, 12)
    opened = GateCalendar(_book([5, 4, 3, 2, 1]), params).on(monday)
    assert opened is not None
    assert opened.score == pytest.approx(1)
    assert opened.gate == 1
    assert opened.obs_date == date(2026, 1, 9)

    two = GateCalendar(_book([5, 4, 3, 2, 1], dxy=[1, 2, 3, 4, 5], vix=[1, 2, 3, 4, 5]), params).on(monday)
    assert two is not None
    assert two.score == pytest.approx(0.5)
    assert two.gate == 0
    assert two.component_dxy == 0
    assert two.component_tips_10y == 1

    three = GateCalendar(_book([5, 4, 3, 2, 1], dxy=[1, 2, 3, 4, 5]), params).on(monday)
    assert three is not None
    assert three.score == pytest.approx(0.75)
    assert three.gate == 1
    assert GateCalendar(_book([5, 4, 3, 2, 1], dxy=[1, 2, 3, 4, 5]), _params(theta=0.75)).on(monday).gate == 1
    assert GateCalendar(_book([5, 4, 3, 2, 1], dxy=[1, 2, 3, 4, 5]), _params(theta=0.76)).on(monday).gate == 0


def test_macro_close_waits_until_the_next_weekday():
    calendar = GateCalendar(_book([5, 4, 3, 2, 100]), _params())
    friday = calendar.on(date(2026, 1, 9))
    monday = calendar.on(date(2026, 1, 12))
    assert friday is not None and monday is not None
    assert friday.obs_date == date(2026, 1, 8)
    assert friday.gate == 1
    assert monday.obs_date == date(2026, 1, 9)
    assert monday.gate == 0
    assert monday.score == 0


def test_weekend_inherits_the_friday_gate():
    calendar = GateCalendar(_book([5, 4, 3, 2, 100]), _params())
    friday = calendar.on(date(2026, 1, 9))
    saturday = calendar.on(date(2026, 1, 10))
    sunday = calendar.on(date(2026, 1, 11))
    assert friday is not None and saturday is not None and sunday is not None
    assert saturday.gate == sunday.gate == friday.gate == 1
    assert saturday.obs_date == sunday.obs_date == friday.obs_date == date(2026, 1, 8)
    assert saturday.effective_date == date(2026, 1, 10)
    assert sunday.effective_date == date(2026, 1, 11)
    rows = calendar.materialize(date(2026, 1, 9), date(2026, 1, 13))
    assert [row.effective_date for row in rows] == [
        date(2026, 1, 9),
        date(2026, 1, 10),
        date(2026, 1, 11),
        date(2026, 1, 12),
    ]
    assert rows[3].obs_date == date(2026, 1, 9)
    assert rows[3].gate == 0


def test_weekend_macro_rows_are_rejected_and_stale_data_closes():
    with pytest.raises(ValueError):
        MacroPoint(date(2026, 1, 10), 1.0)
    calendar = GateCalendar(_book([3, 2, 1]), _params())
    fresh = calendar.on(date(2026, 1, 8))
    stale = calendar.on(date(2026, 2, 20))
    assert fresh is not None and fresh.gate == 1
    assert stale is not None and stale.gate == 0 and stale.score is None

    days = _weekdays(MON, 5)
    full = tuple(MacroPoint(day, value) for day, value in zip(days, [5, 4, 3, 2, 1]))
    thin = MacroBook(dxy=full, tips_10y=full, hy_oas=full, vix=full[:2])
    missing = GateCalendar(thin, _params()).on(date(2026, 1, 12))
    assert missing is not None and missing.gate == 0 and missing.score is None


def test_ema_is_seeded_with_the_first_window_mean():
    assert ema_close([2, 4, 6, 10], 3) == [None, None, 4.0, 7.0]


def test_breakout_without_ema_stays_flat_and_short_is_symmetric():
    params = _params(ema_span=5, structure_days=3)
    rising = _dailies([10, 20, 30, 50, 60])
    rows = daily_bias(rising, params)
    assert rows[3].above_prior_high and rows[3].ema is None and rows[3].bias == "flat"
    assert rows[4].bias == "long" and rows[4].close > rows[4].ema

    params = _params()
    falling = daily_bias(_dailies([50, 40, 30, 20, 10]), params)
    assert falling[4].bias == "short"
    assert falling[4].below_prior_low
    assert falling[4].close < falling[4].ema


def test_hh_hl_still_needs_the_close_above_the_ema():
    params = _params()
    bars = [
        _daily(MON + timedelta(days=offset), close, high, low)
        for offset, close, high, low in (
            (0, 5, 10, 1),
            (1, 8, 12, 3),
            (2, 6, 11, 2),
            (3, 10, 14, 5),
            (4, 9, 13, 4),
            (5, 12, 16, 7),
            (6, 6, 15, 6),
        )
    ]
    rows = daily_bias(bars, params)
    assert rows[6].hh_hl
    assert rows[6].close < rows[6].ema
    assert rows[6].bias == "flat"

    lifted = list(bars)
    lifted[6] = _daily(MON + timedelta(days=6), 11, 15, 6)
    assert daily_bias(lifted, params)[6].bias == "long"


def test_entry_waits_for_an_ema_turn_that_breaks_the_compression_range():
    import dataprice.strategy.sniper.entry as entry

    assert not hasattr(entry, "find_sweep")
    params = _params()
    quiet = _steady_bars(40, start=100, step=0.2, width=1.0)
    assert find_ema_turn(quiet, 39, params) is None
    assert low_atr_ratio(entry_series(quiet, params), 38, params) is not None

    breaking = list(quiet)
    last = breaking[39]
    breaking[39] = Candle(last.open_ts, last.close + 3, last.close + 4, last.low, last.close + 3, 1)
    signal = find_ema_turn(breaking, 39, params)
    assert signal is not None and signal.side == "buy"
    assert breaking[39].close > signal.range_high
    assert signal.atr_ratio <= signal.threshold_ratio
    assert signal.wick == breaking[39].low
    assert find_ema_turn(breaking, 38, params) is None
    assert stop_beyond_wick(signal.side, signal.wick, 0) == signal.wick
    assert stop_beyond_wick("buy", 98, 100) == pytest.approx(97.02)
    assert stop_beyond_wick("sell", 105, 100) == pytest.approx(106.05)

    inside = list(quiet)
    held = inside[39]
    inside[39] = Candle(held.open_ts, held.close + 0.1, held.close + 0.5, held.low, held.close + 0.1, 1)
    assert find_ema_turn(inside, 39, params) is None

    expanded = list(quiet)
    prior = expanded[38]
    expanded[38] = Candle(prior.open_ts, prior.open, prior.close + 30, prior.low, prior.close, 1)
    assert low_atr_ratio(entry_series(expanded, params), 38, params) is None
    assert find_ema_turn(expanded, 39, params) is None

    falling = _steady_bars(40, start=120, step=-0.2, width=1.0)
    assert find_ema_turn(falling, 39, params) is None
    short_bars = list(falling)
    anchor = short_bars[38]
    short_bars[38] = Candle(anchor.open_ts, anchor.close, anchor.close + 0.05, anchor.close - 0.05, anchor.close, 1)
    last = short_bars[39]
    short_bars[39] = Candle(last.open_ts, last.close - 3, last.high, last.close - 4, last.close - 3, 1)
    short = find_ema_turn(short_bars, 39, params)
    assert short is not None and short.side == "sell"
    assert short_bars[39].close < short.range_low
    assert short.wick == short_bars[39].high
    assert short.atr_ratio <= short.threshold_ratio

    locked_bars = _steady_bars(40, start=100, step=0.2, width=1.0)
    assert find_ema_turn(locked_bars, 39, evaluated_params()) is None
    assert find_ema_turn(locked_bars, 33, evaluated_params()) is None


def test_low_atr_threshold_is_the_30th_percentile():
    assert inclusive_percentile(list(range(1, 21)), 0.30) == pytest.approx(6.7)
    assert inclusive_percentile(list(range(1, 21)), 0.50) == pytest.approx(10.5)
    quiet = _steady_bars(40, start=100, step=0.4, width=0.5)
    mild = list(quiet)
    last = mild[39]
    mild[39] = Candle(last.open_ts, last.close, last.close + 0.8, last.close - 0.5, last.close, 1)
    assert low_atr_ratio(entry_series(mild, evaluated_params()), 39, evaluated_params()) is None
    loose = SniperParams(atr_percentile=0.50)
    state = low_atr_ratio(entry_series(mild, loose), 39, loose)
    assert state is not None and state[0] <= state[1]


def test_full_notional_sizes_at_twenty_times_equity_and_three_account_r():
    plan = plan_trade("buy", 102, 98, _params())
    assert plan is not None
    assert plan.qty == Decimal("1960.7843")
    assert plan.qty * plan.entry == pytest.approx(Decimal("200000"), abs=Decimal("0.01"))
    assert plan.entry == Decimal("102")
    assert plan.stop == Decimal("98")
    assert plan.target == Decimal("114")
    assert plan.risk_cash == Decimal("7843.1372")
    assert plan.planned_price_r == pytest.approx(3)
    assert plan.planned_account_r == pytest.approx(3)
    assert plan.leverage_used == pytest.approx(20)

    five = plan_trade("buy", 102, 98, _params(leverage=5))
    assert five is not None and five.qty == Decimal("490.1961")
    assert five.leverage_used == pytest.approx(5)

    golden = plan_trade("buy", 102, 98, _params(min_account_rr=1.618))
    assert golden is not None and golden.qty == plan.qty
    assert golden.target == Decimal("108.472000")
    assert golden.planned_account_r == pytest.approx(1.618)
    ten = plan_trade("buy", 102, 98, _params(min_account_rr=10))
    assert ten is not None
    assert ten.qty == plan.qty
    assert ten.target == Decimal("142")
    assert ten.planned_account_r == pytest.approx(10)

    costly = plan_trade("buy", 102, 98, _params(fee_bps=5, slippage_bps=2))
    assert costly is not None
    assert costly.planned_account_r >= 3
    assert costly.planned_price_r > 3
    assert costly.entry > plan.entry
    assert costly.risk_cash > plan.risk_cash

    tight = plan_trade("buy", 100.01, 99.99, _params())
    assert tight is not None
    assert tight.qty == Decimal("1999.8000")
    assert tight.leverage_used == pytest.approx(20)
    assert tight.planned_account_r >= 3
    assert plan_trade("sell", 100, 140, _params()) is None
    assert plan_trade("sell", 100, 170, _params()) is None
    assert plan_trade("sell", 100, 120, _params(min_account_rr=10)) is None


def test_long_target_short_target_and_stop():
    daily = _dailies([10, 20, 30, 40, 50])
    macro = _book([5, 4, 3, 2, 1])
    signal = _hour4(date(2026, 1, 12), 0, 102, 106, 98, 102)
    target = _hour4(date(2026, 1, 12), 4, 102, 150, 101, 148)
    result = run_sniper(_long_path(signal, target), daily, _params(), macro=macro)
    assert result.skipped == 0
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == "target" and trade.side == "buy" and trade.bias == "long"
    assert trade.entry_ts == signal.open_ts
    assert trade.qty == Decimal("1960.7843")
    assert trade.pnl == q_cash(Decimal("23529.4116"))
    assert trade.account_r == pytest.approx(3)
    assert trade.price_r == pytest.approx(3)
    assert trade.stop == Decimal("98")
    text = format_sniper_report(result, _params())
    assert "价格 R" in text and "账户 R" in text and "v0.1" in text
    assert "全仓" in text and "EMA2" in text and "ATR3" in text and "30 分位" in text and ">= 3" in text

    stopped = run_sniper(
        _long_path(signal, _hour4(date(2026, 1, 12), 4, 102, 110, 90, 100)),
        daily,
        _params(),
        macro=macro,
    )
    assert stopped.trades[0].reason == "stop"
    assert stopped.trades[0].pnl == q_cash(Decimal("-7843.1372"))
    assert stopped.trades[0].account_r == pytest.approx(-1)

    ended = run_sniper(
        _long_path(signal, _hour4(date(2026, 1, 12), 4, 103, 107, 99, 104)),
        daily,
        _params(),
        macro=macro,
    )
    assert ended.trades[0].reason == "end"
    assert ended.trades[0].pnl == q_cash(Decimal("3921.5686"))


def test_gate_blocks_entries_and_does_not_flatten():
    daily = _dailies([10, 20, 30, 40, 50])
    params = _params()
    signal = _hour4(date(2026, 1, 12), 0, 102, 106, 98, 102)
    closed = run_sniper(
        _long_path(signal, _hour4(date(2026, 1, 12), 4, 102, 150, 101, 148)),
        daily,
        params,
        macro=_book([1, 2, 3, 4, 5]),
    )
    assert closed.trades == ()

    held = run_sniper(
        _long_path(
            signal,
            _hour4(date(2026, 1, 13), 0, 103, 107, 99, 104),
            _hour4(date(2026, 1, 14), 0, 104, 160, 100, 150),
        ),
        daily,
        params,
        macro=_book([5, 4, 3, 2, 1, 100]),
    )
    assert len(held.trades) == 1
    assert held.trades[0].entry_ts == signal.open_ts
    assert held.trades[0].reason == "target"
    assert held.trades[0].exit_ts == datetime(2026, 1, 14, tzinfo=UTC)
    assert GateCalendar(_book([5, 4, 3, 2, 1, 100]), params).on(date(2026, 1, 13)).gate == 0


def test_same_day_daily_close_cannot_authorize_the_same_day():
    params = _params()
    bars = _dailies([10, 10, 10, 10, 10]) + [_daily(date(2026, 1, 12), 40, 41, 39)]
    rows = daily_bias(bars, params)
    assert rows[4].day == date(2026, 1, 9) and rows[4].bias == "flat"
    assert rows[5].day == date(2026, 1, 12) and rows[5].bias == "long"
    signal = _hour4(date(2026, 1, 12), 0, 102, 106, 98, 102)
    result = run_sniper(
        _long_path(signal, _hour4(date(2026, 1, 12), 4, 102, 150, 101, 148)),
        bars,
        params,
        macro=_book([5, 4, 3, 2, 1]),
    )
    assert result.trades == ()


def test_short_turn_reaches_three_account_r():
    daily = _dailies([50, 40, 30, 20, 10])
    signal = _hour4(date(2026, 1, 12), 0, 97, 105, 90, 97)
    target = _hour4(date(2026, 1, 12), 4, 90, 96, 10, 20)
    result = run_sniper(_short_path(signal, target), daily, _params(), macro=_book([5, 4, 3, 2, 1]))
    trade = result.trades[0]
    assert trade.side == "sell" and trade.bias == "short" and trade.reason == "target"
    assert trade.entry_ts == signal.open_ts
    assert trade.qty == Decimal("2061.8557")
    assert trade.stop == Decimal("105")
    assert trade.target == Decimal("73")
    assert trade.pnl == q_cash(Decimal("49484.5368"))
    assert trade.account_r == pytest.approx(3)


def test_aggregate_drops_incomplete_buckets():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    hours = [
        Candle(start + timedelta(hours=hour), close, close + 1, close - 1, close, 1)
        for hour, close in enumerate([2, 3, 4, 5])
    ]
    out = to_4h(hours, bar_minutes=60)
    assert len(out) == 1
    assert out[0].open_ts == start
    assert out[0].open == 2 and out[0].close == 5 and out[0].high == 6 and out[0].low == 1
    assert to_4h(hours[:3], bar_minutes=60) == []
    half = [
        Candle(start + timedelta(minutes=30 * step), 1, 2, 0.5, 1, 1)
        for step in range(8)
    ]
    assert len(to_4h(half, bar_minutes=30)) == 1
    day = [
        Candle(start + timedelta(hours=hour), 1, 2, 0.5, 1, 1)
        for hour in range(24)
    ]
    daily = to_daily(day, bar_minutes=60)
    assert len(daily) == 1 and daily[0].open_ts == start
    with pytest.raises(ValueError):
        run_sniper_intraday(hours, _params(), bar_minutes=15, macro=_book([5, 4, 3, 2, 1]))


def test_csv_fred_yahoo_and_factor_frame_round_trip():
    book = parse_macro_csv(
        "series_id,obs_date,close\n"
        "dxy,2026-01-05,100\n"
        "tips_10y,2026-01-05,1.5\n"
        "hy_oas,2026-01-05,3\n"
        "vix,2026-01-05,12\n"
    )
    assert book.dxy[0].close == 100
    assert book.hy_oas[0].obs_date == MON
    with pytest.raises(ValueError):
        parse_macro_csv("series_id,obs_date,close\ndxy,2026-01-10,1\n")
    points = parse_fred_csv("DFII10", "DATE,DFII10\n2026-01-05,1.5\n2026-01-06,.\n2026-01-10,9\n")
    assert [(point.obs_date, point.close) for point in points] == [(MON, 1.5)]
    stamp = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp())
    parsed = parse_yahoo_chart(
        {"chart": {"result": [{"timestamp": [stamp], "indicators": {"quote": [{"close": [12.5]}]}}]}},
        "^VIX",
    )
    assert parsed[0].close == 12.5 and parsed[0].obs_date == MON

    gate = GateCalendar(_book([5, 4, 3, 2, 1]), _params()).on(date(2026, 1, 12))
    frame = factor_frame([gate])
    assert frame.loc[0, "version"] == "v0.1"
    assert int(frame.loc[0, "gate"]) == 1
    restored = gates_from_frame(frame)
    assert restored[0].gate == 1 and restored[0].score == pytest.approx(1)
    assert restored[0].effective_date == date(2026, 1, 12)
    closed = factor_frame([GateCalendar(_book([3, 2, 1]), _params()).on(date(2026, 2, 20))])
    assert gates_from_frame(closed)[0].score is None
    loaded = book_from_frame(
        pd.DataFrame(
            {
                "series_id": ["dxy", "tips_10y", "hy_oas", "vix"],
                "obs_date": [MON, MON, MON, MON],
                "close": [1.0, 2.0, 3.0, 4.0],
            }
        )
    )
    assert loaded.vix[0].close == 4
    assert book_from_frame(pd.DataFrame()).is_empty()
    with pytest.raises(ValueError):
        run_sniper([_hour4(MON, 0, 1, 2, 0.5, 1)], [], _params())


def test_sniper_backtest_is_its_own_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["sniper-backtest", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "一击必杀" in text
    assert "--fee-bps" in text
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "--help"])
    assert exc.value.code == 0
    backtest_help = capsys.readouterr().out
    assert "TPO" in backtest_help
    assert "sniper" not in backtest_help.lower()
    with pytest.raises(SystemExit) as exc:
        main(["macro-backfill", "--help"])
    assert exc.value.code == 0
    assert "v0.1" in capsys.readouterr().out


def _params(**overrides: object) -> SniperParams:
    values: dict[str, object] = {
        "equity": Decimal("10000"),
        "leverage": 20.0,
        "min_account_rr": 3.0,
        "fee_bps": 0.0,
        "slippage_bps": 0.0,
        "ema_fast": 2,
        "ema_mid": 3,
        "ema_slow": 4,
        "ema_slope_bars": 1,
        "atr_length": 3,
        "atr_percentile_window": 3,
        "atr_percentile": 0.30,
        "ema_span": 3,
        "structure_days": 3,
        "pivot_k": 1,
        "macro_lookback": 3,
        "macro_min_obs": 3,
        "macro_max_stale_days": 10,
        "theta": 0.55,
        "stop_buffer_bps": 0.0,
    }
    values.update(overrides)
    return SniperParams(**values)  # type: ignore[arg-type]


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _book(closes: list[float], **replaced: list[float]) -> MacroBook:
    def points(values: list[float]) -> tuple[MacroPoint, ...]:
        return tuple(MacroPoint(day, value) for day, value in zip(_weekdays(MON, len(values)), values))

    return MacroBook(
        dxy=points(replaced.get("dxy", closes)),
        tips_10y=points(replaced.get("tips_10y", closes)),
        hy_oas=points(replaced.get("hy_oas", closes)),
        vix=points(replaced.get("vix", closes)),
    )


def _daily(day: date, close: float, high: float, low: float) -> Candle:
    return Candle(datetime(day.year, day.month, day.day, tzinfo=UTC), close, high, low, close, 1)


def _dailies(closes: list[float]) -> list[Candle]:
    return [
        _daily(_weekdays(MON, len(closes))[index], close, close + 1, close - 1)
        for index, close in enumerate(closes)
    ]


def _hour4(day: date, hour: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(datetime(day.year, day.month, day.day, hour, tzinfo=UTC), open_, high, low, close, 1)


def _steady_bars(count: int, *, start: float, step: float, width: float = 1.0) -> list[Candle]:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[Candle] = []
    for index in range(count):
        close = start + step * index
        bars.append(Candle(opened + timedelta(hours=4 * index), close, close + width, close - width, close, 1))
    return bars


def _weekday_slots() -> list[tuple[date, int]]:
    slots: list[tuple[date, int]] = []
    for day in (date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)):
        for hour in (0, 4, 8, 12, 16, 20):
            slots.append((day, hour))
    return slots


def _long_path(signal: Candle, *after: Candle) -> list[Candle]:
    """1 月 5–7 日窄幅上涨。每根收盘等于上一根最高价，不算走出。信号收盘高于这段高点。"""
    bars: list[Candle] = []
    slots = _weekday_slots()
    last_close = 100.0
    step = 0.5
    first = last_close - step * (len(slots) - 1)
    for index, (day, hour) in enumerate(slots):
        close = first + step * index
        bars.append(_hour4(day, hour, close, close + step, close - step, close))
    return bars + [signal, *after]


def _short_path(signal: Candle, *after: Candle) -> list[Candle]:
    """窄幅下跌，最后一根再收得更窄，它才算压缩。信号收盘跌破这三根的最低价。"""
    bars: list[Candle] = []
    slots = _weekday_slots()
    last_close = 99.0
    step = 0.5
    last = len(slots) - 1
    for index, (day, hour) in enumerate(slots):
        close = last_close + step * (last - index)
        if index == last - 2:
            width = 2.0
        elif index == last - 1:
            width = 1.5
        elif index == last:
            width = 0.2
        else:
            width = 0.5
        bars.append(_hour4(day, hour, close, close + width, close - width, close))
    return bars + [signal, *after]
