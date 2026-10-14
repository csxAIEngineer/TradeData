"""L0 和 L3 之间的唯一桥梁。

preload 在开盘前把账本放进内存。
persist 在别的线程里把出站事件写入 PostgreSQL。
这两件事都不能放进 evaluate / apply_fill。
"""

from __future__ import annotations

from dataprice.config import Settings
from dataprice.contracts import FillEvent, OrderEvent
from dataprice.hotpath.outbox import Outbox
from dataprice.hotpath.state import TradingState
from dataprice.ledger.repo import LedgerRepository, TradingSnapshot, ledger_session


def preload(state: TradingState, ledger: LedgerRepository, account_code: str) -> TradingSnapshot:
    snapshot = ledger.load_trading_snapshot(account_code)
    if not snapshot.loaded or snapshot.max_order_notional is None:
        state.halt()
        return snapshot
    state.load_snapshot(
        positions=list(snapshot.positions),
        halted=snapshot.halted,
        buying_power=snapshot.buying_power,
        max_order_notional=snapshot.max_order_notional,
        max_position_qty=snapshot.max_position_qty,
    )
    return snapshot


def persist(outbox: Outbox, settings: Settings | None = None) -> int:
    events = outbox.take()
    if not events:
        return 0
    try:
        with ledger_session(settings) as ledger:
            for event in events:
                if isinstance(event, OrderEvent):
                    ledger.record_order(event)
                elif isinstance(event, FillEvent):
                    ledger.record_fill(event)
                else:
                    raise TypeError(f"无法落盘的事件: {type(event)!r}")
    except Exception:
        outbox.prepend(events)
        raise
    return len(events)
