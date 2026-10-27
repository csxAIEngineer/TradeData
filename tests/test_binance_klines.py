"""Binance 现货 5 分钟解析，以及缺一半就不合成 10 分钟。不访问网络。"""

import hashlib
import io
import zipfile
from datetime import datetime, timedelta

import pytest

from dataprice.contracts import Bar
from dataprice.feed.binance_flow import archive_requests
from dataprice.feed.binance_history import backfill_spot_klines, fetch_spot_bars
from dataprice.feed.binance_klines import BINANCE_SPOT, aggregate_10m, parse_spot_kline_csv
from dataprice.timeutil import UTC

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 1, 2, tzinfo=UTC)
RECEIVED = datetime(2026, 10, 27, tzinfo=UTC)
MS_OPEN = 1_735_689_600_000
US_OPEN = 1_735_689_600_000_000


def test_spot_archive_uses_the_spot_root():
    urls = archive_requests("BTCUSDT", "5m", START, datetime(2025, 2, 1, tzinfo=UTC), root=BINANCE_SPOT)
    assert urls[0] == (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2025-01.zip",
        True,
    )


def test_millisecond_and_microsecond_opens_land_on_the_same_bar():
    header = "open_time,open,high,low,close,volume,close_time,quote_asset_volume,count,taker,quote,ignore"
    milli = f"{MS_OPEN},100,110,90,105,1.5,{MS_OPEN + 299999},150,10,0.4,40,0"
    micro = f"{US_OPEN},100,110,90,105,2.5,{US_OPEN + 299999999},250,10,0.4,40,0"
    early = parse_spot_kline_csv(
        "\n".join((header, milli)),
        symbol="BTCUSDT",
        freq="5m",
        received_at=RECEIVED,
        start=START,
        end=END,
    )
    later = parse_spot_kline_csv(
        micro,
        symbol="BTCUSDT",
        freq="5m",
        received_at=RECEIVED,
        start=START,
        end=END,
    )
    assert len(early) == 1
    assert early[0].exchange_ts == datetime(2025, 1, 1, tzinfo=UTC)
    assert early[0].amount == pytest.approx(150)
    assert early[0].volume == pytest.approx(1.5)
    assert later[0].exchange_ts == early[0].exchange_ts
    assert later[0].volume == pytest.approx(2.5)


def test_rows_outside_the_window_are_dropped():
    text = f"{MS_OPEN - 300_000},100,110,90,105,1,0,10,1,0,0,0\n{MS_OPEN},100,110,90,105,1,0,10,1,0,0,0"
    rows = parse_spot_kline_csv(
        text,
        symbol="BTCUSDT",
        freq="5m",
        received_at=RECEIVED,
        start=START,
        end=END,
    )
    assert [row.exchange_ts for row in rows] == [START]


def test_ten_minute_bar_needs_both_halves():
    first = _bar(START, close=105, volume=1, amount=10)
    second = _bar(START + timedelta(minutes=5), close=108, high=112, low=95, volume=2, amount=20)
    lone = _bar(START + timedelta(minutes=10), close=101)
    paired = aggregate_10m([second, first])
    assert len(paired) == 1
    assert paired[0].freq == "10m"
    assert paired[0].exchange_ts == START
    assert paired[0].open == pytest.approx(100)
    assert paired[0].close == pytest.approx(108)
    assert paired[0].high == pytest.approx(112)
    assert paired[0].low == pytest.approx(90)
    assert paired[0].volume == pytest.approx(3)
    assert paired[0].amount == pytest.approx(30)
    assert aggregate_10m([first]) == []
    assert aggregate_10m([lone, second]) == []


def test_missing_month_is_skipped_and_a_bad_checksum_is_not_stored():
    blob = _zip(f"{MS_OPEN},100,110,90,105,1.5,0,150,10,0.4,40,0\n")
    digest = hashlib.sha256(blob).hexdigest()
    seen: list[str] = []

    def get_bytes(url: str) -> bytes | None:
        seen.append(url)
        if url.endswith("BTCUSDT-5m-2025-02.zip"):
            return None
        if url.endswith("BTCUSDT-5m-2025-01.zip"):
            return blob
        if url.endswith("BTCUSDT-5m-2025-01.zip.CHECKSUM"):
            return f"{digest}  BTCUSDT-5m-2025-01.zip\n".encode()
        raise AssertionError(url)

    bars, skipped = fetch_spot_bars(
        symbol="BTCUSDT",
        start=START,
        end=datetime(2025, 3, 1, tzinfo=UTC),
        get_bytes=get_bytes,
        sleep_s=0,
        received_at=RECEIVED,
    )
    assert skipped == ("BTCUSDT-5m-2025-02.zip",)
    assert len(bars) == 1
    assert "BTCUSDT-5m-2025-02.zip.CHECKSUM" not in "".join(seen)

    def bad_checksum(url: str) -> bytes | None:
        if url.endswith(".CHECKSUM"):
            return b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  x\n"
        return blob

    with pytest.raises(ValueError, match="校验和不匹配"):
        fetch_spot_bars(
            symbol="BTCUSDT",
            start=START,
            end=datetime(2025, 2, 1, tzinfo=UTC),
            get_bytes=bad_checksum,
            sleep_s=0,
            received_at=RECEIVED,
        )


def test_backfill_refuses_the_okx_symbol_and_an_empty_download():
    with pytest.raises(ValueError, match="BTCUSDT"):
        backfill_spot_klines(symbol="BTC-USDT", get_bytes=lambda _url: None, sleep_s=0)
    with pytest.raises(RuntimeError, match="没有取到"):
        backfill_spot_klines(
            now=datetime(2025, 2, 1, tzinfo=UTC),
            months=1,
            get_bytes=lambda _url: None,
            sleep_s=0,
        )


def _bar(
    ts: datetime,
    *,
    close: float = 100,
    high: float = 110,
    low: float = 90,
    volume: float = 1,
    amount: float = 10,
) -> Bar:
    return Bar(
        symbol="BTCUSDT",
        freq="5m",
        exchange_ts=ts,
        local_recv_ts=RECEIVED,
        open=100,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
    )


def _zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-5m-2025-01.csv", text)
    return buffer.getvalue()
