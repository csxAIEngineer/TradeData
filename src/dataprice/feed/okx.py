"""OKX books5 报文解析。不连接交易所，也不写数据库。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from dataprice.buffer import BatchBuffer
from dataprice.contracts import BookQuote
from dataprice.timeutil import UTC, ensure_aware

log = logging.getLogger(__name__)

OKX_URL = "wss://ws.okx.com:8443/ws/v5/public"
OKX_SOURCE = "okx"
OKX_SYMBOLS = ("BTC-USDT", "ETH-USDT")

# 单源报价超过这个时间就视为陈旧，不再入缓冲。
MAX_AGE_SEC = 3.0
# 连着但没数据，主动重连。
SILENCE_TIMEOUT_SEC = 30.0
# OKX 要应用层文本 ping，协议层 ping 另外开着。
APP_PING_INTERVAL_SEC = 25.0
PING_INTERVAL_SEC = 20
PING_TIMEOUT_SEC = 15
OPEN_TIMEOUT_SEC = 10
BACKOFF_MIN = 1.0
BACKOFF_MAX = 30.0


def subscribe_message() -> str:
    return json.dumps(
        {
            "op": "subscribe",
            "args": [{"channel": "books5", "instId": symbol} for symbol in OKX_SYMBOLS],
        }
    )


def parse_okx_books(raw: str, received_at: datetime) -> list[BookQuote]:
    """解析一条 OKX 文本帧。pong、订阅错误、没有 data 或缺档都返回空列表。"""
    if raw.strip() == "pong":
        return []
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        return []
    if msg.get("event") == "error":
        log.warning("[okx] 订阅错误: %s", msg)
        return []
    if "data" not in msg:
        return []
    arg = msg.get("arg") or {}
    if not isinstance(arg, dict) or arg.get("channel") != "books5":
        return []
    symbol = arg.get("instId")
    if symbol not in OKX_SYMBOLS:
        return []
    rows = msg["data"]
    if not isinstance(rows, list):
        return []
    received_at = ensure_aware(received_at)
    quotes: list[BookQuote] = []
    for row in rows:
        quote = _row_quote(symbol, row, received_at)
        if quote is not None:
            quotes.append(quote)
    return quotes


def push_quote(buffer: BatchBuffer[BookQuote], quote: BookQuote, now: datetime) -> bool:
    """本地接收时间超过 MAX_AGE_SEC 的报价不进入缓冲。"""
    age = (ensure_aware(now) - ensure_aware(quote.local_recv_ts)).total_seconds()
    if age > MAX_AGE_SEC:
        return False
    buffer.add(quote)
    return True


class QuoteLiveness:
    """每个标的最近一次报价。没连上时不报陈旧，调用方只打日志、不写空价格。"""

    def __init__(self, symbols: tuple[str, ...] = OKX_SYMBOLS) -> None:
        self._symbols = symbols
        self._last: dict[str, float] = {}
        self._connected_at: float | None = None

    def connected(self, now_mono: float) -> None:
        self._connected_at = now_mono

    def disconnected(self) -> None:
        self._connected_at = None

    def mark(self, symbol: str, now_mono: float) -> None:
        self._last[symbol] = now_mono

    def quiet_symbols(self, now_mono: float) -> list[str]:
        connected_at = self._connected_at
        if connected_at is None:
            return []
        quiet: list[str] = []
        for symbol in self._symbols:
            baseline = self._last.get(symbol, connected_at)
            if now_mono - baseline > MAX_AGE_SEC:
                quiet.append(symbol)
        return quiet


def _row_quote(symbol: str, row: object, received_at: datetime) -> BookQuote | None:
    if not isinstance(row, dict):
        return None
    bid = _top_px(row.get("bids"))
    ask = _top_px(row.get("asks"))
    if bid is None or ask is None:
        return None
    exchange_ts = _exchange_time(row.get("ts"))
    if exchange_ts is None:
        return None
    return BookQuote(
        symbol=symbol,
        source=OKX_SOURCE,
        exchange_ts=exchange_ts,
        local_recv_ts=received_at,
        bid=bid,
        ask=ask,
    )


def _top_px(levels: object) -> float | None:
    if not isinstance(levels, list) or not levels:
        return None
    top = levels[0]
    if not isinstance(top, (list, tuple)) or len(top) == 0:
        return None
    return float(top[0])


def _exchange_time(ts: object) -> datetime | None:
    if isinstance(ts, bool) or ts is None or ts == "":
        return None
    try:
        millis = int(ts)
    except (TypeError, ValueError):
        return None
    seconds, rem = divmod(millis, 1000)
    return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(milliseconds=rem)
