from decimal import Decimal

import pytest

from dataprice.contracts import Position
from dataprice.hotpath.state import OrderIntent, TradingState


def _state() -> TradingState:
    state = TradingState()
    state.load_snapshot(
        positions=[Position("000001.SZ", Decimal("100"), Decimal("10"))],
        halted=False,
        buying_power=Decimal("10000"),
        max_order_notional=Decimal("5000"),
        max_position_qty=Decimal("150"),
    )
    return state


def test_unloaded_state_rejects():
    decision = TradingState().evaluate(
        OrderIntent("000001.SZ", "buy", Decimal("1"), Decimal("1"))
    )
    assert decision.accepted is False
    assert decision.reason == "halted"


def test_buy_updates_average_cost_and_cash():
    state = _state()
    intent = OrderIntent("000001.SZ", "buy", Decimal("50"), Decimal("13"), Decimal("1"))
    assert state.evaluate(intent).accepted
    position = state.apply_fill(intent)
    assert position is not None
    assert position.qty == Decimal("150.0000")
    assert position.avg_cost == Decimal("11.000000")
    assert state.risk is not None
    assert state.risk.buying_power == Decimal("9349.0000")


def test_buy_rejects_notional_position_and_cash():
    state = _state()
    assert state.evaluate(
        OrderIntent("000001.SZ", "buy", Decimal("600"), Decimal("10"))
    ).reason == "max_order_notional"
    assert state.evaluate(
        OrderIntent("000001.SZ", "buy", Decimal("60"), Decimal("10"))
    ).reason == "max_position_qty"
    tight = TradingState()
    tight.load_snapshot(
        positions=[],
        halted=False,
        buying_power=Decimal("100"),
        max_order_notional=Decimal("5000"),
        max_position_qty=None,
    )
    assert tight.evaluate(
        OrderIntent("000001.SZ", "buy", Decimal("20"), Decimal("10"))
    ).reason == "buying_power"


def test_sell_reduces_position_and_flat_removes_it():
    state = _state()
    assert state.evaluate(
        OrderIntent("000001.SZ", "sell", Decimal("101"), Decimal("11"))
    ).reason == "position"
    left = state.apply_fill(OrderIntent("000001.SZ", "sell", Decimal("40"), Decimal("11")))
    assert left is not None
    assert left.qty == Decimal("60.0000")
    assert left.avg_cost == Decimal("10")
    assert state.risk is not None
    assert state.risk.buying_power == Decimal("10440.0000")
    assert state.apply_fill(OrderIntent("000001.SZ", "sell", Decimal("60"), Decimal("11"))) is None
    assert "000001.SZ" not in state.positions


def test_float_money_is_rejected():
    state = _state()
    decision = state.evaluate(OrderIntent("000001.SZ", "buy", 1.5, Decimal("10")))  # type: ignore[arg-type]
    assert decision.reason == "decimal"
    with pytest.raises(ValueError, match="decimal"):
        state.apply_fill(OrderIntent("000001.SZ", "buy", Decimal("1"), 10.0))  # type: ignore[arg-type]
