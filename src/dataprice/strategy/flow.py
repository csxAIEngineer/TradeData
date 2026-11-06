"""主动成交不平衡。这一层不访问数据库。

扫掉卖侧流动性（低点被影线刺破）时，要看到主动卖出占优：有人在打买单。
扫掉买侧流动性则相反，要看到主动买入占优。没有这根 K 线的成交拆分，
就当这次扫动没有被真实成交流确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from dataprice.timeutil import UTC, ensure_aware

LiquiditySide = str


@dataclass(frozen=True)
class TakerBar:
    """一根 K 线的主动买、主动卖量。``open_ts`` 是开盘时间。"""

    open_ts: datetime
    buy_volume: float
    sell_volume: float

    def __post_init__(self) -> None:
        ensure_aware(self.open_ts)
        if self.buy_volume < 0 or self.sell_volume < 0:
            raise ValueError("主动成交量不能为负")


def imbalance(bar: TakerBar) -> float:
    """主动买减主动卖，再除以两边合计。没有量时是 0。范围大约在 -1 到 1。"""
    total = bar.buy_volume + bar.sell_volume
    if total <= 0:
        return 0.0
    return (bar.buy_volume - bar.sell_volume) / total


def index_flow(rows: Sequence[TakerBar]) -> dict[int, TakerBar]:
    """按开盘毫秒索引。同一根后来的覆盖前面的。"""
    indexed: dict[int, TakerBar] = {}
    for row in rows:
        indexed[open_ms(row.open_ts)] = row
    return indexed


def aggressor_confirms(
    side: LiquiditySide,
    bar: TakerBar | None,
    min_imbalance: float,
) -> bool:
    """``side`` 是被扫掉的流动性一侧，不是下单方向。"""
    if min_imbalance < 0 or min_imbalance > 1:
        raise ValueError("min_imbalance 必须在 0 和 1 之间")
    if bar is None or side not in ("buy", "sell"):
        return False
    if bar.buy_volume + bar.sell_volume <= 0:
        return False
    value = imbalance(bar)
    if side == "sell":
        return value <= -min_imbalance
    return value >= min_imbalance


def open_ms(ts: datetime) -> int:
    aware = ensure_aware(ts).astimezone(UTC)
    return int(round(aware.timestamp() * 1000))


def taker_bars_from_rows(rows: Sequence[object]) -> list[TakerBar]:
    """从查询结果或任意带 open 时间和买卖量的行收成策略用的序列。"""
    bars: list[TakerBar] = []
    for row in rows:
        bars.append(
            TakerBar(
                open_ts=_field(row, "exchange_ts", "open_ts"),
                buy_volume=float(_field(row, "buy_volume")),
                sell_volume=float(_field(row, "sell_volume")),
            )
        )
    bars.sort(key=lambda bar: bar.open_ts)
    return bars


def _field(row: object, *names: str) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
    for name in names:
        if hasattr(row, name):
            return getattr(row, name)
    raise ValueError(f"缺少字段: {names[0]}")
