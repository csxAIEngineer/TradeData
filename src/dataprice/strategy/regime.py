"""行情状态。这一层不访问数据库，也不看未来 K 线。

时段沿用流动性模块的 UTC 划分，21:00 之后记成 ``off``。
效率比是 Kaufman 的路径比：净位移除以绝对走法，1 是单边，0 是来回。
波动用 30 分钟真实波幅的简单均值，再和更早的均值比分位。当前这根
可以进分母的分子，但不能拿以后的波幅来定现在的分位。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from dataprice.strategy.candles import Candle, bar_delta
from dataprice.strategy.liquidity import session_name

SESSIONS = ("asia", "london", "new_york", "off")


def entry_session(ts: datetime) -> str:
    return session_name(ts) or "off"


def efficiency_ratio(closes: Sequence[float]) -> float:
    """``closes`` 至少两根。净变动除以相邻绝对变动之和。"""
    if len(closes) < 2:
        raise ValueError("效率比至少要两根收盘价")
    net = abs(closes[-1] - closes[0])
    path = 0.0
    for prev, cur in zip(closes, closes[1:]):
        path += abs(cur - prev)
    if path <= 0:
        return 0.0
    return net / path


def efficiency_asof(
    higher: Sequence[Candle],
    higher_freq: str,
    lower: Sequence[Candle],
    lower_freq: str,
    bars: int,
) -> tuple[float | None, ...]:
    """低周期收盘时，只用已经收盘的高周期。``bars`` 是收益根数，不是收盘价个数。"""
    if bars < 2:
        raise ValueError("bars 必须 >= 2")
    closes_at = [candle.open_ts + bar_delta(higher_freq) for candle in higher]
    out: list[float | None] = []
    cursor = -1
    for candle in lower:
        known_at = candle.open_ts + bar_delta(lower_freq)
        while cursor + 1 < len(higher) and closes_at[cursor + 1] <= known_at:
            cursor += 1
        if cursor < bars:
            out.append(None)
            continue
        window = [higher[index].close for index in range(cursor - bars, cursor + 1)]
        out.append(efficiency_ratio(window))
    return tuple(out)


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    ranges: list[float] = []
    previous = None
    for candle in candles:
        span = candle.high - candle.low
        if previous is None:
            ranges.append(span)
        else:
            ranges.append(max(span, abs(candle.high - previous), abs(candle.low - previous)))
        previous = candle.close
    return ranges


def atr_percentile(
    candles: Sequence[Candle],
    *,
    length: int = 14,
    lookback: int = 960,
    min_count: int = 48,
) -> tuple[float | None, ...]:
    """每根 K 线的 ATR 在此前 ``lookback`` 根 ATR 里的分位。不够样本就是 None。"""
    if length < 2 or lookback < 2 or min_count < 1:
        raise ValueError("ATR 窗口不合法")
    ranges = true_ranges(candles)
    atrs: list[float | None] = []
    total = 0.0
    for index, value in enumerate(ranges):
        total += value
        if index >= length:
            total -= ranges[index - length]
        atrs.append(total / length if index >= length - 1 else None)
    out: list[float | None] = []
    for index, current in enumerate(atrs):
        if current is None:
            out.append(None)
            continue
        sample = [
            item
            for item in atrs[max(0, index - lookback) : index]
            if item is not None
        ]
        if len(sample) < min_count:
            out.append(None)
            continue
        out.append(sum(item <= current for item in sample) / len(sample))
    return tuple(out)


def trend_side_allows(trade_bias: str, bias_4h: str | None) -> bool:
    """不是猴市时，只顺着已经收盘的 4 小时结构做。

    偏多只做多，偏空只做空。偏向还是 ``neutral``，或者还没有 4 小时结构，就不做。
    """
    if trade_bias == "bull":
        return bias_4h == "bull"
    if trade_bias == "bear":
        return bias_4h == "bear"
    return False


def regime_allows(
    ts: datetime,
    *,
    skip_sessions: Sequence[str] = (),
    er: float | None = None,
    min_er: float | None = None,
    atr_pct: float | None = None,
    min_atr_pct: float | None = None,
    max_atr_pct: float | None = None,
) -> bool:
    """进场这根是否还在允许的行情里。条件没开就通过。样本不够则不通过。"""
    if skip_sessions and entry_session(ts) in skip_sessions:
        return False
    if min_er is not None and (er is None or er < min_er):
        return False
    if min_atr_pct is not None and (atr_pct is None or atr_pct < min_atr_pct):
        return False
    if max_atr_pct is not None and (atr_pct is None or atr_pct > max_atr_pct):
        return False
    return True
