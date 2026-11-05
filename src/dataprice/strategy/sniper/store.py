"""宏观日频和 factor_macro_gate v0.1 的表行。写入仍走 ClickHouse。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from dataprice.strategy.sniper.macro_gate import EffectiveGate, GateCalendar, MacroBook, MacroPoint
from dataprice.strategy.sniper.params import MACRO_FACTORS, MACRO_GATE_VERSION, SniperParams
from dataprice.timeutil import clock_ns

MACRO_COLUMNS = ["series_id", "obs_date", "close", "source", "ingest_ts"]
GATE_COLUMNS = [
    "version",
    "effective_date",
    "obs_date",
    "score",
    "gate",
    "level_dxy",
    "level_tips_10y",
    "level_hy_oas",
    "level_vix",
    "component_dxy",
    "component_tips_10y",
    "component_hy_oas",
    "component_vix",
    "ingest_ts",
]


def macro_frame(book: MacroBook, source: str, ingested: datetime | None = None) -> pd.DataFrame:
    """一条序列一行。``source`` 标 csv、fred 或 yahoo。"""
    stamp = ingested or _now()
    rows = []
    for name in MACRO_FACTORS:
        for point in getattr(book, name):
            rows.append((name, point.obs_date, point.close, source, stamp))
    return pd.DataFrame(rows, columns=MACRO_COLUMNS)


def factor_frame(rows: tuple[EffectiveGate, ...] | list[EffectiveGate], ingested: datetime | None = None) -> pd.DataFrame:
    stamp = ingested or _now()
    records = []
    for row in rows:
        if row.version != MACRO_GATE_VERSION:
            raise ValueError(f"宏观门版本只能是 {MACRO_GATE_VERSION}")
        records.append(
            (
                row.version,
                row.effective_date,
                row.obs_date,
                row.score,
                row.gate,
                row.level_dxy,
                row.level_tips_10y,
                row.level_hy_oas,
                row.level_vix,
                row.component_dxy,
                row.component_tips_10y,
                row.component_hy_oas,
                row.component_vix,
                stamp,
            )
        )
    return pd.DataFrame(records, columns=GATE_COLUMNS)


def materialize_book(
    book: MacroBook,
    params: SniperParams | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[EffectiveGate, ...]:
    """从第一条宏观的次日铺到最后一条之后的第三个自然日，把紧接着的周末也写上。"""
    params = params or SniperParams()
    dates = [point.obs_date for name in MACRO_FACTORS for point in getattr(book, name)]
    if not dates:
        return ()
    first = min(dates) + timedelta(days=1)
    last = max(dates) + timedelta(days=3)
    return GateCalendar(book, params).materialize(start or first, end or last)


def book_from_frame(frame: pd.DataFrame) -> MacroBook:
    if frame.empty:
        return MacroBook.empty()
    buckets: dict[str, list[MacroPoint]] = {name: [] for name in MACRO_FACTORS}
    for row in frame.itertuples(index=False):
        name = str(row.series_id)
        if name not in buckets:
            raise ValueError(f"宏观序列不认识: {name}")
        obs = _as_date(row.obs_date)
        buckets[name].append(MacroPoint(obs, float(row.close)))
    return MacroBook(**{name: tuple(points) for name, points in buckets.items()})


def gates_from_frame(frame: pd.DataFrame) -> tuple[EffectiveGate, ...]:
    if frame.empty:
        return ()
    rows: list[EffectiveGate] = []
    for row in frame.itertuples(index=False):
        rows.append(
            EffectiveGate(
                effective_date=_as_date(row.effective_date),
                obs_date=_as_date(row.obs_date),
                score=_optional_float(row.score),
                gate=int(row.gate),
                version=str(row.version),
                level_dxy=_optional_float(row.level_dxy),
                level_tips_10y=_optional_float(row.level_tips_10y),
                level_hy_oas=_optional_float(row.level_hy_oas),
                level_vix=_optional_float(row.level_vix),
                component_dxy=_optional_float(row.component_dxy),
                component_tips_10y=_optional_float(row.component_tips_10y),
                component_hy_oas=_optional_float(row.component_hy_oas),
                component_vix=_optional_float(row.component_vix),
            )
        )
    return tuple(rows)


def write_macro(book: MacroBook, source: str, settings: object | None = None) -> tuple[int, int]:
    """写入原始日频，并覆盖合成的 v0.1 门。返回 (日频行数, 门行数)。"""
    from dataprice.market.repo import market_session

    params = SniperParams()
    daily = macro_frame(book, source)
    gates = factor_frame(materialize_book(book, params))
    with market_session(settings) as repo:
        daily_n = repo.insert_macro_daily(daily)
        gate_n = repo.insert_factor_macro_gate(gates)
    return daily_n, gate_n


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    stamp = pd.Timestamp(value)
    return stamp.date()


def _optional_float(value: object) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return float(value)


def _now() -> pd.Timestamp:
    return pd.Timestamp(int(clock_ns()), tz="UTC")
