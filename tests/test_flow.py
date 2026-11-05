"""Binance 主动成交归档的解析和回补。不访问网络，也不写 ClickHouse。"""

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from dataprice.feed.binance_flow import archive_requests, parse_binance_kline_csv
from dataprice.feed.flow_history import backfill_taker_flow, fetch_binance_taker_flow
from dataprice.market.validate import validate_taker_flow

UTC = timezone.utc
START = datetime(2026, 10, 20, tzinfo=UTC)
END = datetime(2026, 10, 22, 12, tzinfo=UTC)


def test_archive_uses_monthly_files_then_trailing_days():
    start = datetime(2024, 11, 5, tzinfo=UTC)
    end = datetime(2026, 10, 26, 12, tzinfo=UTC)
    requests = archive_requests("BTCUSDT", "30m", start, end)
    urls = [url for url, _required in requests]
    assert urls[0].endswith("/monthly/klines/BTCUSDT/30m/BTCUSDT-30m-2024-11.zip")
    assert any(url.endswith("BTCUSDT-30m-2026-09.zip") for url in urls)
    assert not any("2026-10.zip" in url for url in urls)
    daily = [(url, required) for url, required in requests if "/daily/" in url]
    assert daily[0][0].endswith("BTCUSDT-30m-2026-10-01.zip")
    assert daily[0][1] is True
    by_name = {url.rsplit("/", 1)[-1]: required for url, required in daily}
    assert by_name["BTCUSDT-30m-2026-10-24.zip"] is True
    assert by_name["BTCUSDT-30m-2026-10-25.zip"] is False
    assert by_name["BTCUSDT-30m-2026-10-26.zip"] is False


def test_parse_splits_taker_buy_and_sell_and_skips_the_header():
    text = "\n".join(
        [
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore",
            _csv(START, volume="10", buy="4", count="12"),
            _csv(END, volume="10", buy="4"),
            "not,a,row",
        ]
    )
    rows = parse_binance_kline_csv(
        text,
        symbol="BTC-USDT",
        source="binance-um",
        freq="30m",
        received_at=END,
        start=START,
        end=END,
    )
    assert len(rows) == 1
    row = validate_taker_flow(rows[0])
    assert row.symbol == "BTC-USDT"
    assert row.source == "binance-um"
    assert row.exchange_ts == START
    assert row.buy_volume == 4
    assert row.sell_volume == 6
    assert row.volume == 10
    assert row.trade_count == 12


def test_parse_rejects_taker_buy_above_total_volume():
    text = _csv(START, volume="1", buy="2")
    assert (
        parse_binance_kline_csv(
            text,
            symbol="BTC-USDT",
            source="binance-um",
            freq="30m",
            received_at=END,
            start=START,
            end=END,
        )
        == []
    )


def test_fetch_skips_unpublished_day_and_checks_the_hash():
    stamp = START
    blob = _zip(_csv(stamp, volume="8", buy="3"))
    digest = hashlib.sha256(blob).hexdigest()

    def get_bytes(url: str) -> bytes | None:
        if url.endswith("2026-10-22.zip"):
            return None
        if url.endswith(".CHECKSUM"):
            return f"{digest}  file.zip\n".encode()
        if url.endswith("2026-10-20.zip") or url.endswith("2026-10-21.zip"):
            return blob
        raise AssertionError(url)

    rows = fetch_binance_taker_flow(
        symbol="BTC-USDT",
        inst_id="BTCUSDT",
        start=START,
        end=END,
        get_bytes=get_bytes,
        sleep_s=0,
        received_at=END,
    )
    assert [row.exchange_ts for row in rows] == [stamp]
    assert rows[0].sell_volume == 5

    def bad_hash(url: str) -> bytes | None:
        if url.endswith(".CHECKSUM"):
            return b"0" * 64
        if url.endswith(".zip"):
            return blob
        return None

    with pytest.raises(ValueError, match="校验和"):
        fetch_binance_taker_flow(
            symbol="BTC-USDT",
            inst_id="BTCUSDT",
            start=START,
            end=START + timedelta(days=1),
            get_bytes=bad_hash,
            sleep_s=0,
            received_at=END,
        )


def test_backfill_inserts_without_clickhouse():
    blob = _zip(_csv(START, volume="8", buy="3"))

    def get_bytes(url: str) -> bytes | None:
        if url.endswith(".CHECKSUM"):
            return None
        if url.endswith(".zip"):
            return blob
        return None

    class Repo:
        def __init__(self) -> None:
            self.rows = []

        def insert_taker_flow(self, batch) -> int:
            self.rows.extend(batch)
            return len(batch)

    repo = Repo()
    written = backfill_taker_flow(
        symbol="BTC-USDT",
        months=1,
        now=END,
        repo=repo,
        get_bytes=get_bytes,
        sleep_s=0,
    )
    assert written == len(repo.rows)
    assert written >= 1
    assert {row.source for row in repo.rows} == {"binance-um"}


def _csv(ts: datetime, *, volume: str, buy: str, count: str = "1") -> str:
    millis = int(ts.timestamp() * 1000)
    return f"{millis},1,2,1,1,{volume},{millis + 1},1,{count},{buy},1,0"


def _zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-30m.csv", text)
    return buffer.getvalue()
