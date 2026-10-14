"""ClickHouse 批量写入和区间查询。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Sequence

import clickhouse_connect
import pandas as pd
from clickhouse_connect.driver.client import Client

from dataprice.buffer import BatchBuffer
from dataprice.config import Settings, get_settings
from dataprice.contracts import Bar, Tick
from dataprice.market.validate import validate_bar, validate_tick
from dataprice.timeutil import clock_ns, ensure_aware

_BAR_COLUMNS = [
    "symbol",
    "freq",
    "exchange_ts",
    "local_recv_ts",
    "ingest_ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
]

_TICK_COLUMNS = [
    "symbol",
    "trade_id",
    "exchange_ts",
    "local_recv_ts",
    "ingest_ts",
    "price",
    "volume",
    "side",
]


class MarketRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def insert_bars(self, bars: Sequence[Bar]) -> int:
        if not bars:
            return 0
        frame = _bars_frame([validate_bar(bar) for bar in bars])
        self._client.insert_df("bars", frame)
        return len(frame)

    def insert_ticks(self, ticks: Sequence[Tick]) -> int:
        if not ticks:
            return 0
        frame = _ticks_frame([validate_tick(tick) for tick in ticks])
        self._client.insert_df("ticks", frame)
        return len(frame)

    def query_bars(
        self,
        symbols: Sequence[str],
        freq: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        if not symbols:
            raise ValueError("symbols 不能为空")
        ensure_aware(start)
        ensure_aware(end)
        frame = self._client.query_df(
            """
            SELECT symbol, freq, exchange_ts, local_recv_ts, ingest_ts,
                   open, high, low, close, volume, amount
            FROM bars FINAL
            WHERE freq = {freq:String}
              AND symbol IN {symbols:Array(String)}
              AND exchange_ts >= {start:DateTime64(9, 'UTC')}
              AND exchange_ts < {end:DateTime64(9, 'UTC')}
            ORDER BY symbol, exchange_ts
            """,
            parameters={
                "freq": freq,
                "symbols": list(symbols),
                "start": _utc_naive(start),
                "end": _utc_naive(end),
            },
        )
        return _localize(frame)

    def query_ticks(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        if not symbols:
            raise ValueError("symbols 不能为空")
        ensure_aware(start)
        ensure_aware(end)
        frame = self._client.query_df(
            """
            SELECT symbol, trade_id, exchange_ts, local_recv_ts, ingest_ts,
                   price, volume, side
            FROM ticks FINAL
            WHERE symbol IN {symbols:Array(String)}
              AND exchange_ts >= {start:DateTime64(9, 'UTC')}
              AND exchange_ts < {end:DateTime64(9, 'UTC')}
            ORDER BY symbol, exchange_ts, trade_id
            """,
            parameters={
                "symbols": list(symbols),
                "start": _utc_naive(start),
                "end": _utc_naive(end),
            },
        )
        return _localize(frame)

    def delete_symbol(self, table: str, symbol: str) -> None:
        if table not in ("bars", "ticks"):
            raise ValueError(table)
        self._client.command(
            f"DELETE FROM {table} WHERE symbol = {{symbol:String}}",
            parameters={"symbol": symbol},
        )


def flush_bars(buffer: BatchBuffer[Bar], repo: MarketRepository) -> int:
    rows = buffer.take()
    if not rows:
        return 0
    try:
        return repo.insert_bars(rows)
    except Exception:
        buffer.prepend(rows)
        raise


def flush_ticks(buffer: BatchBuffer[Tick], repo: MarketRepository) -> int:
    rows = buffer.take()
    if not rows:
        return 0
    try:
        return repo.insert_ticks(rows)
    except Exception:
        buffer.prepend(rows)
        raise


def _bars_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    ingest = _ns_to_utc(clock_ns())
    return pd.DataFrame(
        [
            (
                bar.symbol,
                bar.freq,
                _as_utc(bar.exchange_ts),
                _as_utc(bar.local_recv_ts),
                ingest,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
            )
            for bar in bars
        ],
        columns=_BAR_COLUMNS,
    )


def _ticks_frame(ticks: Sequence[Tick]) -> pd.DataFrame:
    ingest = _ns_to_utc(clock_ns())
    return pd.DataFrame(
        [
            (
                tick.symbol,
                tick.trade_id,
                _as_utc(tick.exchange_ts),
                _as_utc(tick.local_recv_ts),
                ingest,
                tick.price,
                tick.volume,
                tick.side,
            )
            for tick in ticks
        ],
        columns=_TICK_COLUMNS,
    )


def _as_utc(value: datetime) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("时间戳必须带时区")
    return pd.Timestamp(int(stamp.value), tz="UTC")


def _ns_to_utc(ns: int) -> pd.Timestamp:
    return pd.Timestamp(int(ns), tz="UTC")


def _utc_naive(value: datetime) -> datetime:
    """clickhouse-connect 的 DateTime64 参数用 UTC 无时区 datetime。"""
    aware = ensure_aware(value).astimezone(timezone.utc)
    return aware.replace(tzinfo=None)


def _localize(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    for column in ("exchange_ts", "local_recv_ts", "ingest_ts"):
        if column not in out.columns:
            continue
        out[column] = pd.to_datetime(out[column], utc=True)
    return out


@contextmanager
def market_session(settings: Settings | None = None) -> Iterator[MarketRepository]:
    settings = settings or get_settings()
    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_http_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_db,
    )
    try:
        yield MarketRepository(client)
    finally:
        client.close()
