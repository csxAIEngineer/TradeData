"""纯数据约定。这一层没有数据库客户端。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]
OrderStatus = Literal["new", "accepted", "partial", "filled", "canceled", "rejected"]
BarFreq = Literal["1m", "5m", "10m", "15m", "30m", "60m", "1d"]

BAR_FREQS = frozenset({"1m", "5m", "10m", "15m", "30m", "60m", "1d"})
NAME_RE = re.compile(r"^[\w][\w.\-]{0,127}$", re.UNICODE)

QTY_QUANT = Decimal("0.0001")
PRICE_QUANT = Decimal("0.000001")
CASH_QUANT = Decimal("0.0001")


def require_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{name} 要用 Decimal，不能用 float")
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"{name} 必须是 Decimal")
    return value


def q_qty(value: Decimal) -> Decimal:
    return require_decimal(value, "qty").quantize(QTY_QUANT, rounding=ROUND_HALF_UP)


def q_px(value: Decimal) -> Decimal:
    return require_decimal(value, "price").quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


def q_cash(value: Decimal) -> Decimal:
    return require_decimal(value, "cash").quantize(CASH_QUANT, rounding=ROUND_HALF_UP)


def require_name(value: str, label: str) -> str:
    if not isinstance(value, str) or NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 名称不合法: {value!r}")
    return value


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    avg_cost: Decimal


@dataclass(frozen=True)
class Bar:
    symbol: str
    freq: str
    exchange_ts: datetime
    local_recv_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True)
class Tick:
    symbol: str
    trade_id: str
    exchange_ts: datetime
    local_recv_ts: datetime
    price: float
    volume: float
    side: str


@dataclass(frozen=True)
class BookQuote:
    """买卖一价。price 是中间价，spread_bps 是买卖价差（基点）。ingest_ts 落库时再打。"""

    symbol: str
    source: str
    exchange_ts: datetime
    local_recv_ts: datetime
    bid: float
    ask: float

    @property
    def price(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.price * 10_000


@dataclass(frozen=True)
class TakerFlow:
    """一根 K 线上的主动买、主动卖成交量。``exchange_ts`` 是开盘时间。"""

    symbol: str
    source: str
    freq: str
    exchange_ts: datetime
    local_recv_ts: datetime
    buy_volume: float
    sell_volume: float
    volume: float
    trade_count: int


@dataclass(frozen=True)
class OrderEvent:
    """策略线程写入出站队列的订单。ingest_ts 由落库时再打。"""

    client_order_id: str
    account_code: str
    strategy_code: str | None
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal | None
    order_type: OrderType
    status: OrderStatus
    exchange_ts_ns: int | None
    local_recv_ts_ns: int | None


@dataclass(frozen=True)
class FillEvent:
    """成交，并带上这笔成交之后的持仓和可用资金，账本只负责记下。"""

    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal
    exchange_ts_ns: int | None
    local_recv_ts_ns: int | None
    position_qty: Decimal
    avg_cost: Decimal
    buying_power: Decimal
