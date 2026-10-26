"""行情过滤。不连接数据库。"""

from datetime import datetime, timedelta, timezone

import pytest

from dataprice.strategy.candles import Candle, aggregate_candles
from dataprice.strategy.imbalance import find_zones
from dataprice.strategy.liquidity import Level, LiquidityMap, Sweep
from dataprice.strategy.regime import (
    atr_percentile,
    efficiency_asof,
    efficiency_ratio,
    long_sweep_allows,
    regime_allows,
    short_market_allows,
    sweep_rejection_clv,
    trend_side_allows,
    window_net_asof,
)
from dataprice.strategy.signals import (
    StrategyParams,
    bias_asof,
    entry_signals,
    evaluated_params,
    generate_signals,
    long_params,
    regime_params,
    short_regime_params,
    trend_regime_params,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)


def test_efficiency_ratio_is_one_on_a_straight_path_and_low_when_price_retraces():
    assert efficiency_ratio([1, 2, 3, 4]) == pytest.approx(1)
    assert efficiency_ratio([1, 3, 1, 3, 1]) == pytest.approx(0)
    with pytest.raises(ValueError):
        efficiency_ratio([1])


def test_efficiency_asof_waits_for_the_4h_bar_to_close():
    start = datetime(2026, 1, 6, tzinfo=UTC)
    higher = [
        Candle(start, 1, 2, 1, 2),
        Candle(start + timedelta(hours=4), 2, 3, 2, 3),
        Candle(start + timedelta(hours=8), 3, 4, 3, 4),
    ]
    lower = [
        Candle(start + timedelta(hours=7, minutes=30), 1, 1, 1, 1),
        Candle(start + timedelta(hours=12), 1, 1, 1, 1),
    ]
    assert efficiency_asof(higher, "4h", lower, "30m", bars=2) == (None, pytest.approx(1))


def test_atr_percentile_ignores_the_future_and_needs_a_sample():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    calm = [Candle(start + timedelta(minutes=30 * i), 10, 11, 9, 10) for i in range(20)]
    shock = Candle(start + timedelta(minutes=30 * 20), 10, 20, 9, 12)
    later = Candle(start + timedelta(minutes=30 * 21), 12, 13, 11, 12)
    ranks = atr_percentile([*calm, shock, later], length=2, lookback=10, min_count=5)
    assert ranks[5] is None
    assert ranks[20] == pytest.approx(1)
    assert ranks[21] is not None


def test_regime_allows_blocks_a_named_session_and_a_weak_path():
    london = datetime(2026, 1, 6, 9, tzinfo=UTC)
    asia = datetime(2026, 1, 6, 1, tzinfo=UTC)
    assert regime_allows(london, skip_sessions=("asia",)) is True
    assert regime_allows(asia, skip_sessions=("asia",)) is False
    assert regime_allows(london, min_er=0.4, er=0.2) is False
    assert regime_allows(london, min_er=0.4, er=None) is False
    assert regime_allows(london, min_er=0.4, er=0.5) is True
    assert regime_allows(london, max_atr_pct=0.8, atr_pct=0.9) is False
    assert regime_allows(london) is True


def test_regime_profile_keeps_candidate_p_and_adds_the_train_lock():
    params = regime_params()
    base = evaluated_params()
    assert params.confirm_4h is True
    assert params.liquidity_kinds == base.liquidity_kinds
    assert params.max_risk_bps == 80
    assert params.entry == "zone"
    assert params.exit_on_bias_flip is False
    assert params.min_er == 0.25
    assert params.er_bars == 18
    assert params.skip_sessions == ()
    assert base.min_er is None


def test_trend_side_buys_only_in_a_bull_4h_and_sells_only_in_a_bear_4h():
    assert trend_side_allows("bull", "bull") is True
    assert trend_side_allows("bull", "bear") is False
    assert trend_side_allows("bull", "neutral") is False
    assert trend_side_allows("bull", None) is False
    assert trend_side_allows("bear", "bear") is True
    assert trend_side_allows("bear", "bull") is False
    assert trend_side_allows("bear", "neutral") is False


def test_chop_and_4h_direction_gate_the_entry():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    bear = tuple("bear" for _ in candles)
    neutral = tuple("neutral" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    liquidity = LiquidityMap((sell_side, buy_side), (Sweep(5, "sell", sell_side, 97.0),))
    zones = find_zones(candles)
    params = StrategyParams(min_er=0.25, trend_side=True)
    trending = tuple(0.8 for _ in candles)
    chopped = tuple(0.1 for _ in candles)
    bought = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=params,
        bias_4h=bull,
        er_at=trending,
    )
    assert len(bought) == 1
    assert bought[0].side == "buy"
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=params,
            bias_4h=bear,
            er_at=trending,
        )
        == []
    )
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=params,
            bias_4h=neutral,
            er_at=trending,
        )
        == []
    )
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=params,
            bias_4h=bull,
            er_at=chopped,
        )
        == []
    )
    with pytest.raises(ValueError, match="4 小时偏向"):
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=params,
            er_at=trending,
        )


def test_down_market_allows_the_short_and_blocks_it_in_an_up_market():
    candles = _candles(_short_rows())
    bear = tuple("bear" for _ in candles)
    bull = tuple("bull" for _ in candles)
    buy_side = Level("eqh", "buy", 102.0, 0)
    sell_side = Level("eql", "sell", 88.0, 0)
    liquidity = LiquidityMap((buy_side, sell_side), (Sweep(5, "buy", buy_side, 103.0),))
    zones = find_zones(candles)
    params = StrategyParams(min_er=0.25, trend_side=True)
    trending = tuple(0.8 for _ in candles)
    sold = entry_signals(
        candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=liquidity,
        zones=zones,
        params=params,
        bias_4h=bear,
        er_at=trending,
    )
    assert len(sold) == 1
    assert sold[0].side == "sell"
    assert (
        entry_signals(
            candles,
            bias_30=bear,
            bias_1h=bear,
            liquidity=liquidity,
            zones=zones,
            params=params,
            bias_4h=bull,
            er_at=trending,
        )
        == []
    )


def test_trend_regime_profile_stacks_chop_and_direction_on_p():
    params = trend_regime_params()
    assert params.min_er == 0.25
    assert params.er_bars == 18
    assert params.trend_side is True
    assert params.confirm_4h is True
    assert params.liquidity_kinds == evaluated_params().liquidity_kinds
    assert regime_params().trend_side is False


def test_entry_can_skip_the_session_or_a_flat_4h_path():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    liquidity = LiquidityMap((sell_side, buy_side), (Sweep(5, "sell", sell_side, 97.0),))
    zones = find_zones(candles)
    kept = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=StrategyParams(skip_sessions=("asia",)),
    )
    assert len(kept) == 1
    assert kept[0].index == 10
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=StrategyParams(skip_sessions=("new_york",)),
        )
        == []
    )
    flat = tuple(0.1 for _ in candles)
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=StrategyParams(min_er=0.4),
            er_at=flat,
        )
        == []
    )
    assert (
        len(
            entry_signals(
                candles,
                bias_30=bull,
                bias_1h=bull,
                liquidity=liquidity,
                zones=zones,
                params=StrategyParams(min_er=0.4),
                er_at=tuple(0.8 for _ in candles),
            )
        )
        == 1
    )


def test_sweep_clv_measures_where_the_close_sits_in_the_bar():
    high_close = Candle(T0, 100, 101, 97, 100.5)
    weak_close = Candle(T0, 100, 101, 97, 98.2)
    assert sweep_rejection_clv(high_close) == pytest.approx(0.875)
    assert sweep_rejection_clv(weak_close) == pytest.approx(0.3)
    assert sweep_rejection_clv(Candle(T0, 100, 100, 100, 100)) is None
    assert long_sweep_allows(high_close, min_clv=0.7) is True
    assert long_sweep_allows(weak_close, min_clv=0.7) is False
    assert long_sweep_allows(weak_close) is True
    assert long_sweep_allows(Candle(T0, 100, 100, 100, 100), min_clv=0.7) is False


def test_long_clv_blocks_a_weak_sweep_and_leaves_the_short_alone():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    sweep = Sweep(5, "sell", sell_side, 97.0)
    zones = find_zones(candles)
    liquidity = LiquidityMap((sell_side, buy_side), (sweep,))
    kept = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=StrategyParams(long_min_sweep_clv=0.7),
    )
    assert len(kept) == 1
    assert kept[0].side == "buy"
    weak = _candles(_long_rows())
    weak[5] = Candle(weak[5].open_ts, 100, 101, 97, 98.2)
    weak_zones = find_zones(weak)
    assert (
        len(
            entry_signals(
                weak,
                bias_30=bull,
                bias_1h=bull,
                liquidity=liquidity,
                zones=weak_zones,
            )
        )
        == 1
    )
    assert (
        entry_signals(
            weak,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=weak_zones,
            params=StrategyParams(long_min_sweep_clv=0.7),
        )
        == []
    )
    short_candles = _candles(_short_rows())
    bear = tuple("bear" for _ in short_candles)
    short_buy = Level("eqh", "buy", 102.0, 0)
    short_sell = Level("eql", "sell", 88.0, 0)
    sold = entry_signals(
        short_candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=LiquidityMap((short_buy, short_sell), (Sweep(5, "buy", short_buy, 103.0),)),
        zones=find_zones(short_candles),
        params=StrategyParams(long_min_sweep_clv=0.99),
    )
    assert len(sold) == 1
    assert sold[0].side == "sell"


def test_long_profile_is_candidate_p_buys_with_the_train_locked_clv():
    params = long_params()
    base = evaluated_params()
    assert params.sides == ("buy",)
    assert params.long_min_sweep_clv == 0.7
    assert params.confirm_4h is True
    assert params.max_risk_bps == 80
    assert params.liquidity_kinds == ("eqh", "eql")
    assert params.entry == "zone"
    assert params.exit_on_bias_flip is False
    assert params.min_er is None
    assert base.long_min_sweep_clv is None
    assert base.sides == ("buy", "sell")
    assert regime_params().long_min_sweep_clv is None
    with pytest.raises(ValueError, match="long_min_sweep_clv"):
        StrategyParams(long_min_sweep_clv=0)
    with pytest.raises(ValueError, match="long_min_sweep_clv"):
        StrategyParams(long_min_sweep_clv=1.1)


def test_report_splits_train_and_holdout_on_entry_time():
    from decimal import Decimal

    from dataprice.strategy.backtest import BacktestResult, Trade, format_report

    split = datetime(2026, 3, 1, tzinfo=UTC)

    def trade(ts: datetime, pnl: str) -> Trade:
        return Trade(
            side="buy",
            entry_index=1,
            exit_index=2,
            entry_ts=ts,
            exit_ts=ts,
            entry=Decimal("100"),
            exit=Decimal("101"),
            stop=99.0,
            target=110.0,
            reason="target",
            pnl=Decimal(pnl),
            zone_kind="fvg",
        )

    result = BacktestResult(
        trades=(
            trade(datetime(2026, 2, 1, tzinfo=UTC), "2"),
            trade(datetime(2026, 3, 2, tzinfo=UTC), "-1"),
        ),
        signals=2,
        starting_buying_power=Decimal("10"),
        ending_buying_power=Decimal("11"),
    )
    text = format_report(result, split=split)
    assert "训练 平仓 1" in text
    assert "留出 平仓 1" in text
    assert "平仓 2" in format_report(result)

def test_window_net_uses_the_same_closed_4h_window_as_efficiency():
    start = datetime(2026, 1, 6, tzinfo=UTC)
    higher = [
        Candle(start, 1, 2, 1, 2),
        Candle(start + timedelta(hours=4), 2, 3, 2, 3),
        Candle(start + timedelta(hours=8), 3, 4, 1, 1),
    ]
    lower = [
        Candle(start + timedelta(hours=7, minutes=30), 1, 1, 1, 1),
        Candle(start + timedelta(hours=12), 1, 1, 1, 1),
    ]
    assert window_net_asof(higher, "4h", lower, "30m", bars=2) == (None, pytest.approx(-1))
    assert efficiency_asof(higher, "4h", lower, "30m", bars=2)[0] is None


def test_daily_bias_stays_hidden_until_the_utc_day_closes():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    daily = [Candle(start, 10, 11, 9, 10)]
    early = Candle(start + timedelta(hours=23), 10, 10, 10, 10)
    at_close = Candle(start + timedelta(hours=23, minutes=30), 10, 10, 10, 10)
    assert bias_asof(daily, ("bear",), "1d", [early], "30m") == ("neutral",)
    assert bias_asof(daily, ("bear",), "1d", [at_close], "30m") == ("bear",)


def test_aggregate_keeps_a_full_utc_day_and_drops_a_partial_one():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    full = [
        Candle(start + timedelta(hours=hour), 10, 12, 9, 11, 1)
        for hour in range(24)
    ]
    partial = full[:-1]
    assert len(aggregate_candles(full, "1d")) == 1
    assert aggregate_candles(full, "1d")[0].open_ts == start
    assert aggregate_candles(full, "1d")[0].high == 12
    assert aggregate_candles(full, "1d")[0].low == 9
    assert aggregate_candles(full, "1d")[0].close == 11
    assert aggregate_candles(partial, "1d") == []


def test_short_market_allows_needs_a_closed_daily_bear_and_a_down_path():
    assert short_market_allows(
        daily_bias="bear",
        require_daily_bear=True,
        er=0.4,
        min_er=0.25,
        net=-1,
        require_down=True,
    )
    assert not short_market_allows(
        daily_bias="bull",
        require_daily_bear=True,
        er=0.8,
        min_er=None,
        net=-1,
        require_down=False,
    )
    assert not short_market_allows(
        daily_bias="neutral",
        require_daily_bear=True,
        er=0.8,
        min_er=None,
        net=-1,
        require_down=False,
    )
    assert not short_market_allows(
        daily_bias="bear",
        require_daily_bear=True,
        er=0.1,
        min_er=0.25,
        net=-1,
        require_down=False,
    )
    assert not short_market_allows(
        daily_bias="bear",
        require_daily_bear=True,
        er=None,
        min_er=0.25,
        net=-1,
        require_down=False,
    )
    assert not short_market_allows(
        daily_bias="bear",
        require_daily_bear=True,
        er=0.8,
        min_er=0.25,
        net=1,
        require_down=True,
    )
    assert not short_market_allows(
        daily_bias="bear",
        require_daily_bear=False,
        er=0.8,
        min_er=None,
        net=None,
        require_down=True,
    )


def test_short_gate_keeps_the_buy_and_drops_a_nominal_bear_short():
    long_candles = _candles(_long_rows())
    bull = tuple("bull" for _ in long_candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    long_liquidity = LiquidityMap((sell_side, buy_side), (Sweep(5, "sell", sell_side, 97.0),))
    long_zones = find_zones(long_candles)
    hostile = StrategyParams(short_daily_bias=True, short_min_er=0.25, short_net_down=True)
    bought = entry_signals(
        long_candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=long_liquidity,
        zones=long_zones,
        params=hostile,
        bias_1d=tuple("bull" for _ in long_candles),
        short_er_at=tuple(0.05 for _ in long_candles),
        short_net_at=tuple(1.0 for _ in long_candles),
    )
    plain_buy = entry_signals(
        long_candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=long_liquidity,
        zones=long_zones,
    )
    assert [signal.side for signal in bought] == ["buy"]
    assert [signal.index for signal in bought] == [signal.index for signal in plain_buy]

    short_candles = _candles(_short_rows())
    bear = tuple("bear" for _ in short_candles)
    short_buy = Level("eqh", "buy", 102.0, 0)
    short_sell = Level("eql", "sell", 88.0, 0)
    short_liquidity = LiquidityMap(
        (short_buy, short_sell),
        (Sweep(5, "buy", short_buy, 103.0),),
    )
    short_zones = find_zones(short_candles)
    plain_sell = entry_signals(
        short_candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=short_liquidity,
        zones=short_zones,
    )
    assert [signal.side for signal in plain_sell] == ["sell"]
    blocked = entry_signals(
        short_candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=short_liquidity,
        zones=short_zones,
        params=StrategyParams(short_daily_bias=True),
        bias_1d=tuple("bull" for _ in short_candles),
    )
    assert blocked == []
    kept = entry_signals(
        short_candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=short_liquidity,
        zones=short_zones,
        params=StrategyParams(short_daily_bias=True, short_min_er=0.25, short_net_down=True),
        bias_1d=bear,
        short_er_at=tuple(0.8 for _ in short_candles),
        short_net_at=tuple(-1.0 for _ in short_candles),
    )
    assert [signal.index for signal in kept] == [signal.index for signal in plain_sell]
    chopped = entry_signals(
        short_candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=short_liquidity,
        zones=short_zones,
        params=StrategyParams(short_daily_bias=True, short_min_er=0.25),
        bias_1d=bear,
        short_er_at=tuple(0.1 for _ in short_candles),
    )
    assert chopped == []
    rally = entry_signals(
        short_candles,
        bias_30=bear,
        bias_1h=bear,
        liquidity=short_liquidity,
        zones=short_zones,
        params=StrategyParams(short_daily_bias=True, short_net_down=True),
        bias_1d=bear,
        short_net_at=tuple(2.0 for _ in short_candles),
    )
    assert rally == []
    with pytest.raises(ValueError, match="日线偏向"):
        entry_signals(
            short_candles,
            bias_30=bear,
            bias_1h=bear,
            liquidity=short_liquidity,
            zones=short_zones,
            params=StrategyParams(short_daily_bias=True),
        )


def test_generate_signals_short_gate_does_not_change_buys():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    candles_30 = _candles([(100, 101, 99, 100)] * 96, start=start)
    candles_60 = _candles([(100, 101, 99, 100)] * 48, start=start, minutes=60)
    plain = generate_signals(candles_30, candles_60, StrategyParams())
    gated = generate_signals(
        candles_30,
        candles_60,
        StrategyParams(short_daily_bias=True, short_min_er=0.25, short_net_down=True),
    )
    assert [signal for signal in gated.signals if signal.side == "buy"] == [
        signal for signal in plain.signals if signal.side == "buy"
    ]
    assert {signal.index for signal in gated.signals if signal.side == "sell"} <= {
        signal.index for signal in plain.signals if signal.side == "sell"
    }


def test_short_regime_profile_filters_sells_only_and_leaves_p_long_rules():
    params = short_regime_params()
    base = evaluated_params()
    assert params.short_min_er == 0.25
    assert params.short_er_bars == 18
    assert params.short_er_freq == "4h"
    assert params.short_daily_bias is False
    assert params.short_net_down is False
    assert params.min_er is None
    assert params.trend_side is False
    assert params.confirm_4h is True
    assert params.liquidity_kinds == base.liquidity_kinds
    assert params.max_risk_bps == 80
    assert params.entry == "zone"
    assert params.exit_on_bias_flip is False
    assert params.sides == base.sides


def test_short_params_reject_a_bad_efficiency_floor():
    with pytest.raises(ValueError, match="short_min_er"):
        StrategyParams(short_min_er=0)
    with pytest.raises(ValueError, match="short_er_freq"):
        StrategyParams(short_er_freq="30m")  # type: ignore[arg-type]
    assert evaluated_params().short_daily_bias is False
    assert evaluated_params().short_min_er is None
    assert regime_params().short_net_down is False


def _candles(rows, start=T0, minutes=30):
    return [
        Candle(start + timedelta(minutes=minutes * index), open_, high, low, close, 1.0)
        for index, (open_, high, low, close) in enumerate(rows)
    ]


def _short_rows():
    """做多夹具的价格反射。扫买侧流动性，再回到看空失衡。"""
    return [(200 - open_, 200 - low, 200 - high, 200 - close) for open_, high, low, close in _long_rows()]


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
