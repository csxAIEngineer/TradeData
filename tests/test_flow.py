"""主动成交解析和扫流动性过滤。不访问网络，也不写 ClickHouse。"""

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from dataprice.feed.binance_flow import archive_requests, parse_binance_kline_csv
from dataprice.feed.flow_history import backfill_taker_flow, fetch_binance_taker_flow
from dataprice.market.validate import validate_taker_flow
from dataprice.strategy.flow import TakerBar, aggressor_confirms, imbalance, index_flow
from dataprice.strategy.imbalance import find_zones
from dataprice.strategy.liquidity import Level, LiquidityMap, Sweep
from dataprice.strategy.candles import Candle
from dataprice.strategy.signals import StrategyParams, entry_signals, flow_params, generate_signals

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)
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


def test_sell_side_sweep_needs_taker_selling():
    sold = TakerBar(START, buy_volume=40, sell_volume=60)
    bought = TakerBar(START, buy_volume=70, sell_volume=30)
    assert imbalance(sold) == pytest.approx(-0.2)
    assert aggressor_confirms("sell", sold, 0.1) is True
    assert aggressor_confirms("sell", bought, 0.1) is False
    assert aggressor_confirms("buy", bought, 0.1) is True
    assert aggressor_confirms("sell", None, 0.1) is False
    flat = TakerBar(START, buy_volume=0, sell_volume=0)
    assert aggressor_confirms("sell", flat, 0.0) is False


def test_entry_drops_a_sweep_without_agreeing_flow():
    candles = _candles(_long_rows())
    bull = tuple("bull" for _ in candles)
    sell_side = Level("eql", "sell", 98.0, 0)
    buy_side = Level("eqh", "buy", 112.0, 0)
    sweep = Sweep(5, "sell", sell_side, 97.0)
    liquidity = LiquidityMap((sell_side, buy_side), (sweep,))
    zones = find_zones(candles)
    params = StrategyParams(min_flow_imbalance=0.1)
    kept = entry_signals(
        candles,
        bias_30=bull,
        bias_1h=bull,
        liquidity=liquidity,
        zones=zones,
        params=params,
        flow_at=index_flow([TakerBar(candles[5].open_ts, 40, 60)]),
    )
    assert len(kept) == 1
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=params,
            flow_at=index_flow([TakerBar(candles[5].open_ts, 70, 30)]),
        )
        == []
    )
    assert (
        entry_signals(
            candles,
            bias_30=bull,
            bias_1h=bull,
            liquidity=liquidity,
            zones=zones,
            params=params,
            flow_at={},
        )
        == []
    )


def test_generate_signals_requires_flow_when_the_filter_is_on():
    candles = _candles(_structure_rows())
    higher = _candles([(9, 16, 7, 12)], start=T0, minutes=60)
    with pytest.raises(ValueError, match="主动成交"):
        generate_signals(candles, higher, StrategyParams(wing=1, min_flow_imbalance=0.1))
    assert flow_params().min_flow_imbalance == 0.0
    assert flow_params().liquidity_kinds == ("eqh", "eql")


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


def _candles(rows, start=T0, minutes=30):
    return [
        Candle(start + timedelta(minutes=minutes * index), open_, high, low, close, 1.0)
        for index, (open_, high, low, close) in enumerate(rows)
    ]


def _long_rows():
    return [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 97, 100.5),
        (100.5, 102, 100.2, 101.5),
        (101.5, 105, 101.4, 104.5),
        (104.5, 106, 103.2, 105.5),
        (105.5, 106, 105, 105.2),
        (105.2, 105.4, 103.0, 104.0),
        (104, 113, 103.5, 111),
    ]


def _structure_rows():
    return [
        (9, 10, 8, 9),
        (8, 11, 7, 8),
        (11, 12, 9, 11),
        (13, 14, 10, 13),
        (12, 13, 11, 12),
        (15, 16, 12, 15),
    ]


def _csv(ts: datetime, *, volume: str, buy: str, count: str = "1") -> str:
    millis = int(ts.timestamp() * 1000)
    return f"{millis},1,2,1,1,{volume},{millis + 1},1,{count},{buy},1,0"


def _zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-30m.csv", text)
    return buffer.getvalue()
