"""进程内持仓和风控标志。evaluate / apply_fill 不产生任何 IO。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dataprice.contracts import Position, Side, q_cash, q_px, q_qty, require_name


@dataclass
class RiskFlags:
    halted: bool
    buying_power: Decimal
    max_order_notional: Decimal
    max_position_qty: Decimal | None


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str


class TradingState:
    """启动时由 session.preload 填入。盘中只改这块内存。"""

    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}
        self.risk: RiskFlags | None = None

    def load_snapshot(
        self,
        *,
        positions: list[Position],
        halted: bool,
        buying_power: Decimal,
        max_order_notional: Decimal,
        max_position_qty: Decimal | None,
    ) -> None:
        buying_power = q_cash(buying_power)
        max_order_notional = q_cash(max_order_notional)
        if buying_power < 0:
            raise ValueError("buying_power 不能为负")
        if max_order_notional <= 0:
            raise ValueError("max_order_notional 必须大于 0")
        if max_position_qty is not None:
            max_position_qty = q_qty(max_position_qty)
            if max_position_qty <= 0:
                raise ValueError("max_position_qty 必须大于 0")
        self.positions = {}
        for position in positions:
            if position.qty == 0:
                continue
            self.positions[position.symbol] = position
        self.risk = RiskFlags(
            halted=halted,
            buying_power=buying_power,
            max_order_notional=max_order_notional,
            max_position_qty=max_position_qty,
        )

    def halt(self) -> None:
        """未加载风控，或显式停机。失败关闭，不放行订单。"""
        self.risk = RiskFlags(
            halted=True,
            buying_power=Decimal("0"),
            max_order_notional=Decimal("1"),
            max_position_qty=Decimal("0"),
        )
        self.positions = {}

    def evaluate(self, intent: OrderIntent) -> Decision:
        priced = _priced(intent)
        if isinstance(priced, Decision):
            return priced
        symbol, side, qty, price, fee = priced
        if self.risk is None or self.risk.halted:
            return Decision(False, "halted")
        notional = q_cash(qty * price)
        if notional > self.risk.max_order_notional:
            return Decision(False, "max_order_notional")
        held = self.positions[symbol].qty if symbol in self.positions else Decimal("0")
        if side == "buy":
            if (
                self.risk.max_position_qty is not None
                and held + qty > self.risk.max_position_qty
            ):
                return Decision(False, "max_position_qty")
        elif held < qty:
            return Decision(False, "position")
        delta = _cash_delta(side, qty, price, fee)
        if delta < 0 and -delta > self.risk.buying_power:
            return Decision(False, "buying_power")
        return Decision(True, "ok")

    def apply_fill(self, intent: OrderIntent) -> Position | None:
        """先判断，再改内存。返回更新后的持仓；平仓后返回 None。"""
        decision = self.evaluate(intent)
        if not decision.accepted:
            raise ValueError(decision.reason)
        assert self.risk is not None
        priced = _priced(intent)
        if isinstance(priced, Decision):
            raise ValueError(priced.reason)
        symbol, side, qty, price, fee = priced
        previous = self.positions.get(symbol)
        held = previous.qty if previous else Decimal("0")
        avg = previous.avg_cost if previous else Decimal("0")
        if side == "buy":
            new_qty = q_qty(held + qty)
            new_avg = q_px((avg * held + price * qty) / new_qty)
        else:
            new_qty = q_qty(held - qty)
            new_avg = avg if new_qty != 0 else Decimal("0")
        self.risk.buying_power = q_cash(self.risk.buying_power + _cash_delta(side, qty, price, fee))
        if new_qty == 0:
            self.positions.pop(symbol, None)
            return None
        updated = Position(symbol=symbol, qty=new_qty, avg_cost=new_avg)
        self.positions[symbol] = updated
        return updated


def _priced(
    intent: OrderIntent,
) -> Decision | tuple[str, Side, Decimal, Decimal, Decimal]:
    try:
        symbol = require_name(intent.symbol, "标的")
    except ValueError:
        return Decision(False, "symbol")
    if intent.side not in ("buy", "sell"):
        return Decision(False, "side")
    try:
        qty = q_qty(intent.qty)
        price = q_px(intent.price)
        fee = q_cash(intent.fee)
    except TypeError:
        return Decision(False, "decimal")
    if qty <= 0:
        return Decision(False, "qty")
    if price <= 0:
        return Decision(False, "price")
    if fee < 0:
        return Decision(False, "fee")
    return symbol, intent.side, qty, price, fee


def _cash_delta(side: Side, qty: Decimal, price: Decimal, fee: Decimal) -> Decimal:
    if side == "buy":
        return -q_cash(qty * price + fee)
    return q_cash(qty * price - fee)
