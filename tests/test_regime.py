"""行情过滤。不连接数据库。"""

from datetime import datetime, timedelta, timezone

import pytest

from dataprice.strategy.candles import Candle
from dataprice.strategy.imbalance import find_zones
from dataprice.strategy.liquidity import Level, LiquidityMap, Sweep
from dataprice.strategy.regime import (
    atr_percentile,
    efficiency_asof,
    efficiency_ratio,
    regime_allows,
)
from dataprice.strategy.signals import StrategyParams, entry_signals, evaluated_params, regime_params

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


def _candles(rows, start=T0, minutes=30):
    return [
        Candle(start + timedelta(minutes=minutes * index), open_, high, low, close, 1.0)
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
