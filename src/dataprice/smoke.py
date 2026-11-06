"""对三个存储各做一轮写入和读回。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from dataprice.buffer import BatchBuffer
from dataprice.config import get_settings
from dataprice.contracts import Bar, BookQuote, FillEvent, OrderEvent, Position, Tick
from dataprice.factors.store import FactorStore
from dataprice.hotpath.outbox import Outbox
from dataprice.hotpath.state import OrderIntent, TradingState
from dataprice.ledger.repo import ledger_session
from dataprice.market.repo import flush_bars, flush_quotes, flush_ticks, market_session
from dataprice.session import persist, preload
from dataprice.timeutil import clock_ns, to_ns

log = logging.getLogger(__name__)

ACCOUNT = "SMOKE"
STRATEGY = "smoke_strategy"
SYMBOL = "SMOKE"
FACTOR = "smoke_mom"


def smoke() -> None:
    from dataprice.bootstrap import bootstrap

    settings = get_settings()
    bootstrap(settings)
    _ledger(settings)
    _market()
    _factors()
    log.info("验收通过：PostgreSQL、ClickHouse、ArcticDB 都可以读写")


def _ledger(settings) -> None:
    with ledger_session(settings) as ledger:
        ledger.delete_account(ACCOUNT)
        ledger.delete_strategy(STRATEGY)
        ledger.ensure_account(ACCOUNT, "验收账户", "local")
        ledger.ensure_strategy(STRATEGY, "验收策略", {"lookback": 20})
        ledger.set_cash(ACCOUNT, Decimal("1000000"))
        ledger.set_risk(
            ACCOUNT,
            halted=False,
            max_order_notional=Decimal("200000"),
            max_position_qty=Decimal("10000"),
        )
        ledger.upsert_position(
            ACCOUNT,
            Position(SYMBOL, Decimal("100"), Decimal("10")),
        )

    state = TradingState()
    with ledger_session(settings) as ledger:
        snapshot = preload(state, ledger, ACCOUNT)
    if not snapshot.loaded:
        raise RuntimeError("风控快照没有载入内存")
    assert state.risk is not None
    assert state.risk.buying_power == Decimal("1000000.0000")
    assert state.positions[SYMBOL].qty == Decimal("100.0000")

    rejected = state.evaluate(
        OrderIntent(SYMBOL, "buy", Decimal("100000"), Decimal("10"))
    )
    if rejected.accepted or rejected.reason != "max_order_notional":
        raise RuntimeError(f"超限订单应该被拒绝，实际 {rejected}")

    intent = OrderIntent(SYMBOL, "buy", Decimal("100"), Decimal("12"), Decimal("1"))
    decision = state.evaluate(intent)
    if not decision.accepted:
        raise RuntimeError(f"正常订单被拒绝: {decision.reason}")
    position = state.apply_fill(intent)
    assert position is not None
    if position.qty != Decimal("200.0000") or position.avg_cost != Decimal("11.000000"):
        raise RuntimeError(f"持仓计算错误: {position}")
    if state.risk.buying_power != Decimal("998799.0000"):
        raise RuntimeError(f"可用资金计算错误: {state.risk.buying_power}")

    now_ns = clock_ns()
    outbox = Outbox()
    outbox.add(
        OrderEvent(
            client_order_id="smoke-order-1",
            account_code=ACCOUNT,
            strategy_code=STRATEGY,
            symbol=SYMBOL,
            side="buy",
            qty=Decimal("100"),
            price=Decimal("12"),
            order_type="limit",
            status="filled",
            exchange_ts_ns=now_ns,
            local_recv_ts_ns=now_ns,
        )
    )
    outbox.add(
        FillEvent(
            client_order_id="smoke-order-1",
            symbol=SYMBOL,
            side="buy",
            qty=Decimal("100"),
            price=Decimal("12"),
            fee=Decimal("1"),
            exchange_ts_ns=now_ns,
            local_recv_ts_ns=now_ns,
            position_qty=position.qty,
            avg_cost=position.avg_cost,
            buying_power=state.risk.buying_power,
        )
    )
    written = persist(outbox, settings)
    if written != 2 or len(outbox) != 0:
        raise RuntimeError("出站事件没有落盘")

    with ledger_session(settings) as ledger:
        check = preload(TradingState(), ledger, ACCOUNT)
        ledger.delete_account(ACCOUNT)
        ledger.delete_strategy(STRATEGY)
    if check.buying_power != Decimal("998799.0000"):
        raise RuntimeError(f"账本资金没有跟上内存: {check.buying_power}")
    held = {item.symbol: item.qty for item in check.positions}
    if held.get(SYMBOL) != Decimal("200.0000"):
        raise RuntimeError(f"账本持仓没有跟上内存: {held}")
    log.info("PostgreSQL 账本：预加载、内存成交、异步落盘读回一致")


def _market() -> None:
    exchange = pd.Timestamp("2024-01-02 01:30:00.000000001", tz="UTC")
    received = exchange + pd.Timedelta(microseconds=250)
    later = exchange + pd.Timedelta(minutes=1)
    bars = BatchBuffer[Bar](high_watermark=100)
    bars.add(Bar(SYMBOL, "1m", exchange, received, 10, 11, 9, 10.5, 1000, 10500))
    bars.add(Bar(SYMBOL, "1m", later, later, 10.5, 12, 10, 11, 800, 8800))
    ticks = BatchBuffer[Tick](high_watermark=100)
    ticks.add(Tick(SYMBOL, "smoke-1", exchange, received, 10.5, 100, "buy"))
    sample = BookQuote(SYMBOL, "okx", exchange, received, 10.0, 10.5)
    quotes = BatchBuffer[BookQuote](high_watermark=100)
    quotes.add(sample)
    with market_session() as market:
        try:
            flush_bars(bars, market)
            flush_ticks(ticks, market)
            flush_quotes(quotes, market)
            start = datetime(2024, 1, 2, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            got = market.query_bars([SYMBOL], "1m", start, end)
            got_ticks = market.query_ticks([SYMBOL], start, end)
            got_quotes = market.query_quotes([SYMBOL], start, end)
        finally:
            market.delete_symbol("bars", SYMBOL)
            market.delete_symbol("ticks", SYMBOL)
            market.delete_symbol("quotes", SYMBOL)
    if len(got) != 2:
        raise RuntimeError(f"K 线读回行数是 {len(got)}")
    back_ns = to_ns(pd.Timestamp(got.iloc[0]["exchange_ts"]))
    if back_ns != int(exchange.value):
        raise RuntimeError(f"纳秒时间戳丢了: 写入 {int(exchange.value)} 读回 {back_ns}")
    if len(got_ticks) != 1 or got_ticks.iloc[0]["trade_id"] != "smoke-1":
        raise RuntimeError(f"逐笔读回异常: {got_ticks}")
    if len(got_quotes) != 1:
        raise RuntimeError(f"买卖一价读回行数是 {len(got_quotes)}")
    row = got_quotes.iloc[0]
    if row["source"] != "okx" or float(row["bid"]) != sample.bid or float(row["ask"]) != sample.ask:
        raise RuntimeError(f"买卖一价读回异常: {row.to_dict()}")
    price_off = abs(float(row["price"]) - sample.price) > 1e-9
    spread_off = abs(float(row["spread_bps"]) - sample.spread_bps) > 1e-6
    if price_off or spread_off:
        raise RuntimeError(
            f"中间价或价差读回异常: price={row['price']} spread_bps={row['spread_bps']}"
        )
    quote_ns = to_ns(pd.Timestamp(row["exchange_ts"]))
    if quote_ns != int(exchange.value):
        raise RuntimeError(f"买卖一价纳秒时间戳丢了: 写入 {int(exchange.value)} 读回 {quote_ns}")
    log.info("ClickHouse 行情：K 线、逐笔和买卖一价批量写入后按标的读回，纳秒时间戳保持不变")


def _factors() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-01-02", tz="Asia/Shanghai"),
            pd.Timestamp("2024-01-03", tz="Asia/Shanghai"),
        ],
        name="exchange_ts",
    )
    first = pd.DataFrame({"SMOKE": [1.0, 1.1], "SMOKE2": [2.0, 2.2]}, index=index)
    revised = first.copy()
    revised["SMOKE"] = [9.0, 9.1]
    store = FactorStore()
    try:
        store.delete_cross_section(FACTOR)
        store.delete_symbol_factors(SYMBOL)
        first_version = store.write_cross_section(FACTOR, first)
        second_version = store.write_cross_section(FACTOR, revised)
        old = store.read_cross_section(FACTOR, as_of=first_version)
        latest = store.read_cross_section(FACTOR)
        visible = store.read_cross_section(
            FACTOR,
            as_of=first_version,
            decision_ts=pd.Timestamp("2024-01-02", tz="Asia/Shanghai"),
        )
        series = pd.DataFrame({"smoke_mom": [1.0, 1.1]}, index=index)
        store.write_symbol_factors(SYMBOL, series)
        back = store.read_symbol_factors(SYMBOL)
    finally:
        store.delete_cross_section(FACTOR)
        store.delete_symbol_factors(SYMBOL)
    if old.version != first_version or float(old.frame.iloc[0]["SMOKE"]) != 1.0:
        raise RuntimeError(f"as_of 没有读到第一版: version={old.version} value={old.frame.iloc[0]['SMOKE']}")
    if latest.version != second_version or float(latest.frame.iloc[0]["SMOKE"]) != 9.0:
        raise RuntimeError("最新版本不是改写后的因子")
    if len(visible.frame) != 1:
        raise RuntimeError(f"decision_ts 没有截断未来行: {len(visible.frame)}")
    if list(back.frame.columns) != ["smoke_mom"]:
        raise RuntimeError(f"时序面板列不对: {list(back.frame.columns)}")
    log.info("ArcticDB 因子：横截面版本、决策时点截断、标的时序都读得回来")
