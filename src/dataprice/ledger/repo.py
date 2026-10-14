"""账户、资金、持仓、订单、成交。金额用 Decimal，时间用纳秒整数。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from dataprice.config import Settings, get_settings
from dataprice.contracts import FillEvent, OrderEvent, Position, q_cash, q_qty
from dataprice.timeutil import clock_ns


@dataclass(frozen=True)
class TradingSnapshot:
    account_code: str
    currency: str
    buying_power: Decimal
    halted: bool
    max_order_notional: Decimal | None
    max_position_qty: Decimal | None
    positions: tuple[Position, ...]
    loaded: bool


class LedgerRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def ensure_account(self, code: str, name: str, broker: str, currency: str = "CNY") -> int:
        row = self._conn.execute(
            """
            INSERT INTO ledger.accounts (code, name, broker, currency)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE
            SET name = EXCLUDED.name, updated_at = now()
            RETURNING account_id
            """,
            (code, name, broker, currency),
        ).fetchone()
        assert row is not None
        return int(row["account_id"])

    def ensure_strategy(self, code: str, name: str, params: dict | None = None) -> int:
        body = params or {}
        row = self._conn.execute(
            """
            INSERT INTO ledger.strategies (code, name, params)
            VALUES (%s, %s, %s)
            ON CONFLICT (code) DO NOTHING
            RETURNING strategy_id
            """,
            (code, name, psycopg.types.json.Jsonb(body)),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT strategy_id FROM ledger.strategies WHERE code = %s",
                (code,),
            ).fetchone()
        assert row is not None
        strategy_id = int(row["strategy_id"])
        self._conn.execute(
            """
            INSERT INTO ledger.strategy_param_history (strategy_id, version, params)
            VALUES (%s, 1, %s)
            ON CONFLICT (strategy_id, version) DO NOTHING
            """,
            (strategy_id, psycopg.types.json.Jsonb(body)),
        )
        return strategy_id

    def set_cash(self, account_code: str, available: Decimal, frozen: Decimal = Decimal("0")) -> None:
        available = q_cash(available)
        frozen = q_cash(frozen)
        if available < 0 or frozen < 0:
            raise ValueError("资金不能为负")
        account = self._account(account_code)
        previous = self._conn.execute(
            "SELECT available FROM ledger.cash_balances WHERE account_id = %s",
            (account["account_id"],),
        ).fetchone()
        before = previous["available"] if previous else Decimal("0")
        self._conn.execute(
            """
            INSERT INTO ledger.cash_balances (account_id, currency, available, frozen)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE
            SET available = EXCLUDED.available,
                frozen = EXCLUDED.frozen,
                updated_at = now()
            """,
            (account["account_id"], account["currency"], available, frozen),
        )
        delta = q_cash(available - before)
        if delta != 0 or previous is None:
            self._journal(account["account_id"], delta, available, "snapshot", None, None)

    def set_risk(
        self,
        account_code: str,
        *,
        halted: bool,
        max_order_notional: Decimal,
        max_position_qty: Decimal | None,
    ) -> None:
        max_order_notional = q_cash(max_order_notional)
        if max_order_notional <= 0:
            raise ValueError("max_order_notional 必须大于 0")
        if max_position_qty is not None:
            max_position_qty = q_qty(max_position_qty)
            if max_position_qty <= 0:
                raise ValueError("max_position_qty 必须大于 0")
        account = self._account(account_code)
        self._conn.execute(
            """
            INSERT INTO ledger.risk_limits (
                account_id, halted, max_order_notional, max_position_qty
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE
            SET halted = EXCLUDED.halted,
                max_order_notional = EXCLUDED.max_order_notional,
                max_position_qty = EXCLUDED.max_position_qty,
                updated_at = now()
            """,
            (account["account_id"], halted, max_order_notional, max_position_qty),
        )

    def upsert_position(self, account_code: str, position: Position) -> None:
        account = self._account(account_code)
        self._conn.execute(
            """
            INSERT INTO ledger.positions (account_id, symbol, qty, avg_cost)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, symbol) DO UPDATE
            SET qty = EXCLUDED.qty, avg_cost = EXCLUDED.avg_cost, updated_at = now()
            """,
            (account["account_id"], position.symbol, position.qty, position.avg_cost),
        )

    def record_order(self, event: OrderEvent) -> int:
        account = self._account(event.account_code)
        strategy_id = self._strategy_id(event.strategy_code)
        row = self._conn.execute(
            """
            INSERT INTO ledger.orders (
                client_order_id, account_id, strategy_id, symbol, side, qty, price,
                order_type, status, exchange_ts_ns, local_recv_ts_ns, ingest_ts_ns
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING order_id
            """,
            (
                event.client_order_id,
                account["account_id"],
                strategy_id,
                event.symbol,
                event.side,
                event.qty,
                event.price,
                event.order_type,
                event.status,
                event.exchange_ts_ns,
                event.local_recv_ts_ns,
                clock_ns(),
            ),
        ).fetchone()
        assert row is not None
        return int(row["order_id"])

    def record_fill(self, event: FillEvent) -> int:
        order = self._conn.execute(
            """
            SELECT order_id, account_id
            FROM ledger.orders
            WHERE client_order_id = %s
            """,
            (event.client_order_id,),
        ).fetchone()
        if order is None:
            raise LookupError(f"订单不存在: {event.client_order_id}")
        ingest_ts_ns = clock_ns()
        row = self._conn.execute(
            """
            INSERT INTO ledger.fills (
                order_id, qty, price, fee, exchange_ts_ns, local_recv_ts_ns, ingest_ts_ns
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING fill_id
            """,
            (
                order["order_id"],
                event.qty,
                event.price,
                event.fee,
                event.exchange_ts_ns,
                event.local_recv_ts_ns,
                ingest_ts_ns,
            ),
        ).fetchone()
        assert row is not None
        self._conn.execute(
            """
            INSERT INTO ledger.positions (account_id, symbol, qty, avg_cost)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, symbol) DO UPDATE
            SET qty = EXCLUDED.qty, avg_cost = EXCLUDED.avg_cost, updated_at = now()
            """,
            (order["account_id"], event.symbol, event.position_qty, event.avg_cost),
        )
        cash = self._conn.execute(
            "SELECT available, currency FROM ledger.cash_balances WHERE account_id = %s",
            (order["account_id"],),
        ).fetchone()
        if cash is None:
            raise LookupError(f"账户还没有资金行: {event.client_order_id}")
        before = cash["available"]
        buying_power = q_cash(event.buying_power)
        self._conn.execute(
            """
            UPDATE ledger.cash_balances
            SET available = %s, updated_at = now()
            WHERE account_id = %s
            """,
            (buying_power, order["account_id"]),
        )
        self._journal(
            order["account_id"],
            q_cash(buying_power - before),
            buying_power,
            "fill",
            "order",
            event.client_order_id,
            ingest_ts_ns,
        )
        return int(row["fill_id"])

    def load_trading_snapshot(self, account_code: str) -> TradingSnapshot:
        account = self._account(account_code)
        cash = self._conn.execute(
            "SELECT available FROM ledger.cash_balances WHERE account_id = %s",
            (account["account_id"],),
        ).fetchone()
        risk = self._conn.execute(
            """
            SELECT halted, max_order_notional, max_position_qty
            FROM ledger.risk_limits
            WHERE account_id = %s
            """,
            (account["account_id"],),
        ).fetchone()
        rows = self._conn.execute(
            """
            SELECT symbol, qty, avg_cost
            FROM ledger.positions
            WHERE account_id = %s AND qty <> 0
            ORDER BY symbol
            """,
            (account["account_id"],),
        ).fetchall()
        positions = tuple(
            Position(symbol=row["symbol"], qty=row["qty"], avg_cost=row["avg_cost"])
            for row in rows
        )
        if risk is None:
            return TradingSnapshot(
                account_code=account_code,
                currency=account["currency"],
                buying_power=Decimal("0"),
                halted=True,
                max_order_notional=None,
                max_position_qty=None,
                positions=positions,
                loaded=False,
            )
        return TradingSnapshot(
            account_code=account_code,
            currency=account["currency"],
            buying_power=cash["available"] if cash else Decimal("0"),
            halted=bool(risk["halted"]),
            max_order_notional=risk["max_order_notional"],
            max_position_qty=risk["max_position_qty"],
            positions=positions,
            loaded=True,
        )

    def delete_account(self, account_code: str) -> None:
        """只给验收脚本清数据。按外键从叶子往根删。"""
        row = self._conn.execute(
            "SELECT account_id FROM ledger.accounts WHERE code = %s",
            (account_code,),
        ).fetchone()
        if row is None:
            return
        account_id = row["account_id"]
        self._conn.execute(
            """
            DELETE FROM ledger.fills
            WHERE order_id IN (SELECT order_id FROM ledger.orders WHERE account_id = %s)
            """,
            (account_id,),
        )
        self._conn.execute("DELETE FROM ledger.orders WHERE account_id = %s", (account_id,))
        self._conn.execute("DELETE FROM ledger.positions WHERE account_id = %s", (account_id,))
        self._conn.execute("DELETE FROM ledger.cash_journal WHERE account_id = %s", (account_id,))
        self._conn.execute("DELETE FROM ledger.cash_balances WHERE account_id = %s", (account_id,))
        self._conn.execute("DELETE FROM ledger.risk_limits WHERE account_id = %s", (account_id,))
        self._conn.execute("DELETE FROM ledger.accounts WHERE account_id = %s", (account_id,))

    def delete_strategy(self, code: str) -> None:
        row = self._conn.execute(
            "SELECT strategy_id FROM ledger.strategies WHERE code = %s",
            (code,),
        ).fetchone()
        if row is None:
            return
        strategy_id = row["strategy_id"]
        self._conn.execute(
            "DELETE FROM ledger.strategy_param_history WHERE strategy_id = %s",
            (strategy_id,),
        )
        self._conn.execute("DELETE FROM ledger.strategies WHERE strategy_id = %s", (strategy_id,))

    def _account(self, code: str) -> dict:
        row = self._conn.execute(
            "SELECT account_id, currency FROM ledger.accounts WHERE code = %s",
            (code,),
        ).fetchone()
        if row is None:
            raise LookupError(f"账户不存在: {code}")
        return row

    def _strategy_id(self, code: str | None) -> int | None:
        if code is None:
            return None
        row = self._conn.execute(
            "SELECT strategy_id FROM ledger.strategies WHERE code = %s",
            (code,),
        ).fetchone()
        if row is None:
            raise LookupError(f"策略不存在: {code}")
        return int(row["strategy_id"])

    def _journal(
        self,
        account_id: int,
        amount: Decimal,
        available_after: Decimal,
        reason: str,
        ref_type: str | None,
        ref_id: str | None,
        ingest_ts_ns: int | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ledger.cash_journal (
                account_id, amount, available_after, reason, ref_type, ref_id, ingest_ts_ns
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                account_id,
                amount,
                available_after,
                reason,
                ref_type,
                ref_id,
                clock_ns() if ingest_ts_ns is None else ingest_ts_ns,
            ),
        )


@contextmanager
def ledger_session(settings: Settings | None = None) -> Iterator[LedgerRepository]:
    settings = settings or get_settings()
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        row_factory=dict_row,
        connect_timeout=5,
    ) as conn:
        yield LedgerRepository(conn)
