"""因子面板的形状检查。索引是“值变得可知的时间”，不是报告期。"""

from __future__ import annotations

import pandas as pd

from dataprice.contracts import require_name


def validate_panel(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("面板必须是 DataFrame")
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("面板必须有行和列")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("索引必须是 DatetimeIndex，名字用 exchange_ts")
    if frame.index.tz is None:
        raise ValueError("exchange_ts 必须带时区，避免把公告日写成朴素时间")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("exchange_ts 必须按时间升序")
    if not frame.index.is_unique:
        raise ValueError("exchange_ts 不能重复")
    if frame.index.name not in (None, "exchange_ts"):
        raise ValueError("索引名必须是 exchange_ts")
    out = frame.copy()
    out.index.name = "exchange_ts"
    if out.columns.has_duplicates:
        raise ValueError("列名重复")
    for column in out.columns:
        if not isinstance(column, str):
            raise ValueError(f"列名必须是字符串: {column!r}")
        require_name(column, "列")
        if not pd.api.types.is_numeric_dtype(out[column]):
            raise ValueError(f"列 {column} 必须是数值")
    return out


def slice_as_of(frame: pd.DataFrame, decision_ts: pd.Timestamp | None) -> pd.DataFrame:
    """只保留 decision_ts 及之前已经可知的行。"""
    if decision_ts is None:
        return frame
    stamp = pd.Timestamp(decision_ts)
    if stamp.tzinfo is None:
        raise ValueError("decision_ts 必须带时区")
    if frame.index.tz is not None:
        stamp = stamp.tz_convert(frame.index.tz)
    return frame.loc[:stamp]
