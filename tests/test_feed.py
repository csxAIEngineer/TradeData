import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from dataprice.buffer import BatchBuffer
from dataprice.contracts import BookQuote
from dataprice.feed.okx import (
    OKX_URL,
    QuoteLiveness,
    parse_okx_books,
    push_quote,
    subscribe_message,
)
from dataprice.market.validate import validate_quote

NOW = datetime(2024, 1, 2, 8, 30, tzinfo=timezone.utc)
EXCHANGE_TS = "1704153600123"
EXCHANGE = datetime(2024, 1, 2, 0, 0, 0, 123000, tzinfo=timezone.utc)


def _books(symbol: str, *, bids, asks, ts: str | None = EXCHANGE_TS) -> str:
    row = {"bids": bids, "asks": asks}
    if ts is not None:
        row["ts"] = ts
    return json.dumps(
        {
            "arg": {"channel": "books5", "instId": symbol},
            "data": [row],
        }
    )


@pytest.mark.parametrize("symbol", ["BTC-USDT", "ETH-USDT"])
def test_books5_uses_top_of_book(symbol):
    raw = json.dumps(
        {
            "arg": {"channel": "books5", "instId": symbol},
            "data": [
                {
                    "bids": [["99", "1.5", "0", "2"], ["98", "3"]],
                    "asks": [["101", "0.4", "0", "1"], ["102", "2"]],
                    "ts": EXCHANGE_TS,
                },
                {"bids": [], "asks": [["101", "1"]], "ts": EXCHANGE_TS},
            ],
        }
    )
    quotes = parse_okx_books(raw, NOW)
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.symbol == symbol
    assert quote.source == "okx"
    assert quote.bid == 99
    assert quote.ask == 101
    assert quote.price == 100
    assert quote.spread_bps == 200
    assert quote.exchange_ts == EXCHANGE
    assert quote.local_recv_ts == NOW


@pytest.mark.parametrize("raw", ["pong", "pong\n", "  pong  "])
def test_pong_is_ignored(raw):
    assert parse_okx_books(raw, NOW) == []


def test_subscribe_error_is_ignored(caplog):
    caplog.set_level(logging.WARNING, logger="dataprice.feed.okx")
    raw = json.dumps({"event": "error", "code": "60012", "msg": "Invalid request"})
    assert parse_okx_books(raw, NOW) == []
    assert "订阅错误" in caplog.text


def test_subscribe_ack_without_data_is_ignored():
    raw = json.dumps(
        {"event": "subscribe", "arg": {"channel": "books5", "instId": "BTC-USDT"}}
    )
    assert parse_okx_books(raw, NOW) == []


def test_other_instrument_is_ignored():
    assert parse_okx_books(_books("SOL-USDT", bids=[["1", "1"]], asks=[["2", "1"]]), NOW) == []


@pytest.mark.parametrize(
    "bids,asks,ts",
    [
        ([], [["101", "1"]], EXCHANGE_TS),
        ([["99", "1"]], [], EXCHANGE_TS),
        (None, [["101", "1"]], EXCHANGE_TS),
        ([["99", "1"]], None, EXCHANGE_TS),
        ([["99", "1"]], [["101", "1"]], None),
    ],
)
def test_incomplete_book_is_ignored(bids, asks, ts):
    row = {}
    if bids is not None:
        row["bids"] = bids
    if asks is not None:
        row["asks"] = asks
    if ts is not None:
        row["ts"] = ts
    raw = json.dumps({"arg": {"channel": "books5", "instId": "BTC-USDT"}, "data": [row]})
    assert parse_okx_books(raw, NOW) == []


def test_subscribe_message_is_okx_books5():
    message = json.loads(subscribe_message())
    assert OKX_URL == "wss://ws.okx.com:8443/ws/v5/public"
    assert message == {
        "op": "subscribe",
        "args": [
            {"channel": "books5", "instId": "BTC-USDT"},
            {"channel": "books5", "instId": "ETH-USDT"},
        ],
    }


def test_valid_quote_passes():
    quote = BookQuote("BTC-USDT", "okx", NOW, NOW, 99, 101)
    assert validate_quote(quote) is quote
    assert quote.price == 100
    assert quote.spread_bps == 200
    locked = BookQuote("ETH-USDT", "okx", NOW, NOW, 10, 10)
    assert validate_quote(locked) is locked
    assert locked.spread_bps == 0


@pytest.mark.parametrize("bid,ask", [(11, 10), (0, 1), (-1, 2)])
def test_crossed_or_non_positive_book_is_rejected(bid, ask):
    quote = BookQuote("BTC-USDT", "okx", NOW, NOW, bid, ask)
    with pytest.raises(ValueError, match="bid"):
        validate_quote(quote)


@pytest.mark.parametrize(
    "exchange_ts,local_recv_ts",
    [
        (datetime(2024, 1, 2), NOW),
        (NOW, datetime(2024, 1, 2)),
    ],
)
def test_quote_without_timezone_is_rejected(exchange_ts, local_recv_ts):
    quote = BookQuote("BTC-USDT", "okx", exchange_ts, local_recv_ts, 99, 101)
    with pytest.raises(ValueError, match="时区"):
        validate_quote(quote)


def test_illegal_symbol_is_rejected():
    with pytest.raises(ValueError, match="标的"):
        validate_quote(BookQuote("BTC USDT", "okx", NOW, NOW, 99, 101))


def test_stale_quote_is_not_buffered():
    now = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)
    buffer = BatchBuffer[BookQuote](4)
    fresh = BookQuote("BTC-USDT", "okx", now, now - timedelta(seconds=3), 99, 101)
    late = BookQuote(
        "ETH-USDT",
        "okx",
        now,
        now - timedelta(seconds=3, microseconds=1),
        99,
        101,
    )
    older = BookQuote("ETH-USDT", "okx", now, now - timedelta(seconds=4), 99, 101)
    assert push_quote(buffer, fresh, now) is True
    assert push_quote(buffer, late, now) is False
    assert push_quote(buffer, older, now) is False
    assert buffer.take() == [fresh]


def test_source_quiet_after_three_seconds_without_a_quote():
    health = QuoteLiveness()
    assert health.quiet_symbols(100.0) == []
    health.connected(0.0)
    assert health.quiet_symbols(3.0) == []
    assert health.quiet_symbols(3.5) == ["BTC-USDT", "ETH-USDT"]
    health.mark("BTC-USDT", 3.5)
    assert health.quiet_symbols(6.5) == ["ETH-USDT"]
    assert health.quiet_symbols(7.0) == ["BTC-USDT", "ETH-USDT"]
    health.disconnected()
    assert health.quiet_symbols(100.0) == []
