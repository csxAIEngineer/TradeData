"""MacroGate v0.1。

四个日频因子等权：DXY、10Y TIPS 实际利率、HY OAS、VIX。数值越高，金融条件越紧。
某个因子的最新点不高于自己最近 ``macro_lookback`` 个点的中位数，记 1，否则记 0。
分数是四个 0/1 的平均数。分数 >= θ（0.55）时 GATE=1，否则 GATE=0。
四个因子里有三个不高于中位数是 0.75，两个是 0.50。θ 落在中间，所以至少要三个。

t 日收盘只从下一个工作日的会话开始生效。周六和周日不重新计算，沿用周五已经生效的门。
周五生效的门来自周四（或更早一个仍新鲜的）收盘，不是周五当天的收盘。
周一用周五收盘。因子缺数据、点数不够、或相对会话日已经陈旧，门就是 0。
GATE=0 只挡住新开仓。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from dataprice.strategy.sniper.params import MACRO_FACTORS, MACRO_GATE_VERSION, SniperParams


@dataclass(frozen=True)
class MacroPoint:
    """一个工作日的收盘。``obs_date`` 是这根收盘所属的日期。"""

    obs_date: date
    close: float

    def __post_init__(self) -> None:
        if self.obs_date.weekday() >= 5:
            raise ValueError(f"宏观序列不收周末: {self.obs_date}")
        if not math.isfinite(self.close):
            raise ValueError("宏观收盘必须是有限数")


@dataclass(frozen=True)
class MacroBook:
    """四条日频收盘。信号逻辑只读这个对象，不读数据库。"""

    dxy: tuple[MacroPoint, ...] = ()
    tips_10y: tuple[MacroPoint, ...] = ()
    hy_oas: tuple[MacroPoint, ...] = ()
    vix: tuple[MacroPoint, ...] = ()

    def __post_init__(self) -> None:
        for name in MACRO_FACTORS:
            _check_series(getattr(self, name), name)

    def is_empty(self) -> bool:
        return all(not getattr(self, name) for name in MACRO_FACTORS)

    @staticmethod
    def empty() -> MacroBook:
        return MacroBook()


@dataclass(frozen=True)
class EffectiveGate:
    """某个 UTC 日可以用来决定新开仓的门。"""

    effective_date: date
    obs_date: date
    score: float | None
    gate: int
    version: str
    level_dxy: float | None = None
    level_tips_10y: float | None = None
    level_hy_oas: float | None = None
    level_vix: float | None = None
    component_dxy: float | None = None
    component_tips_10y: float | None = None
    component_hy_oas: float | None = None
    component_vix: float | None = None

    def __post_init__(self) -> None:
        if self.version != MACRO_GATE_VERSION:
            raise ValueError(f"宏观门版本只能是 {MACRO_GATE_VERSION}")
        if self.gate not in (0, 1):
            raise ValueError("gate 只能是 0 或 1")
        if self.gate == 1 and (self.score is None or self.score < 0):
            raise ValueError("GATE=1 必须有分数")


def risk_on_component(closes: tuple[float, ...] | list[float]) -> float:
    """最新点不高于窗口中位数则是 1。窗口含最新点。"""
    if len(closes) < 3:
        raise ValueError("至少要 3 个点才能算中位数")
    latest = closes[-1]
    if not all(math.isfinite(value) for value in closes):
        raise ValueError("宏观收盘必须是有限数")
    return 1.0 if latest <= median(closes) else 0.0


class GateCalendar:
    """由已加载的日频序列算出每个会话日的门。"""

    def __init__(self, book: MacroBook, params: SniperParams | None = None) -> None:
        self.params = params or SniperParams()
        self._series = {name: getattr(book, name) for name in MACRO_FACTORS}
        dates: set[date] = set()
        for points in self._series.values():
            dates.update(point.obs_date for point in points)
        self.obs_dates = tuple(sorted(dates))
        self._rows = {day: self._score(day) for day in self.obs_dates}

    def on(self, session: date) -> EffectiveGate | None:
        """``session`` 当天新开仓用哪一扇门。没有更早的宏观点时返回 None，调用方当关门。"""
        if session.weekday() >= 5:
            friday = session - timedelta(days=session.weekday() - 4)
            inherited = self.on(friday)
            if inherited is None:
                return None
            return _with_effective(inherited, session)
        if not self.obs_dates:
            return None
        index = bisect.bisect_left(self.obs_dates, session) - 1
        if index < 0:
            return None
        obs = self.obs_dates[index]
        scored = self._rows[obs]
        if (session - obs).days > self.params.macro_max_stale_days:
            return _closed(session, obs, self.params.macro_version)
        return _with_effective(scored, session)

    def materialize(self, start: date, end: date) -> tuple[EffectiveGate, ...]:
        """``end`` 不含。只写出找得到宏观依据的会话日，含周末沿用。"""
        if end <= start:
            return ()
        rows: list[EffectiveGate] = []
        day = start
        while day < end:
            row = self.on(day)
            if row is not None:
                rows.append(row)
            day += timedelta(days=1)
        return tuple(rows)

    def _score(self, obs: date) -> EffectiveGate:
        levels: dict[str, float] = {}
        components: dict[str, float] = {}
        for name, points in self._series.items():
            window = _window(points, obs, self.params.macro_lookback)
            if len(window) < self.params.macro_min_obs:
                return _closed(obs, obs, self.params.macro_version)
            latest = window[-1]
            if (obs - latest.obs_date).days > self.params.macro_max_stale_days:
                return _closed(obs, obs, self.params.macro_version)
            levels[name] = latest.close
            components[name] = risk_on_component([point.close for point in window])
        score = sum(components.values()) / len(MACRO_FACTORS)
        gate = 1 if score >= self.params.theta else 0
        return EffectiveGate(
            effective_date=obs,
            obs_date=obs,
            score=score,
            gate=gate,
            version=self.params.macro_version,
            level_dxy=levels["dxy"],
            level_tips_10y=levels["tips_10y"],
            level_hy_oas=levels["hy_oas"],
            level_vix=levels["vix"],
            component_dxy=components["dxy"],
            component_tips_10y=components["tips_10y"],
            component_hy_oas=components["hy_oas"],
            component_vix=components["vix"],
        )


def parse_macro_csv(text: str) -> MacroBook:
    """读 ``series_id,obs_date,close``。``series_id`` 是 dxy、tips_10y、hy_oas、vix。"""
    import csv
    from io import StringIO

    buckets: dict[str, list[MacroPoint]] = {name: [] for name in MACRO_FACTORS}
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("宏观 CSV 没有表头")
    fields = {name.strip() for name in reader.fieldnames}
    missing = {"series_id", "obs_date", "close"} - fields
    if missing:
        raise ValueError(f"宏观 CSV 缺少列: {sorted(missing)}")
    for line, row in enumerate(reader, start=2):
        name = (row.get("series_id") or "").strip()
        if name not in buckets:
            raise ValueError(f"第 {line} 行 series_id 不认识: {name}")
        raw_day = (row.get("obs_date") or "").strip()
        raw_close = (row.get("close") or "").strip()
        try:
            obs = date.fromisoformat(raw_day)
            close = float(raw_close)
        except ValueError as exc:
            raise ValueError(f"第 {line} 行日期或收盘无法解析") from exc
        buckets[name].append(MacroPoint(obs, close))
    return MacroBook(**{name: tuple(points) for name, points in buckets.items()})


def _check_series(points: tuple[MacroPoint, ...], name: str) -> None:
    last: date | None = None
    for point in points:
        if last is not None and point.obs_date <= last:
            raise ValueError(f"{name} 必须按日期严格升序且不重复")
        last = point.obs_date


def _window(points: tuple[MacroPoint, ...], obs: date, lookback: int) -> tuple[MacroPoint, ...]:
    dates = [point.obs_date for point in points]
    end = bisect.bisect_right(dates, obs)
    start = max(0, end - lookback)
    return points[start:end]


def _closed(effective: date, obs: date, version: str) -> EffectiveGate:
    return EffectiveGate(
        effective_date=effective,
        obs_date=obs,
        score=None,
        gate=0,
        version=version,
    )


def _with_effective(row: EffectiveGate, session: date) -> EffectiveGate:
    if row.effective_date == session:
        return row
    return EffectiveGate(
        effective_date=session,
        obs_date=row.obs_date,
        score=row.score,
        gate=row.gate,
        version=row.version,
        level_dxy=row.level_dxy,
        level_tips_10y=row.level_tips_10y,
        level_hy_oas=row.level_hy_oas,
        level_vix=row.level_vix,
        component_dxy=row.component_dxy,
        component_tips_10y=row.component_tips_10y,
        component_hy_oas=row.component_hy_oas,
        component_vix=row.component_vix,
    )
