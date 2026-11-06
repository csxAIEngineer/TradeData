"""OKX 历史 K 线解析和分页。不访问网络，也不写 ClickHouse。"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from dataprice.feed.history import backfill, fetch_okx_bars, insert_history
from dataprice.feed.okx_candles import okx_bar, parse_okx_candle_payload
from dataprice.market.validate import validate_bar

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 2, tzinfo=timezone.utc)


def _row(ts: datetime, *, confirm: str = "1", close: str = "10.5") -> list[str]:
    return [str(int(ts.timestamp() * 1000)), "10", "11", "9", close, "1.5", "15", "16.2", confirm]


def test_okx_bar_names():
    assert okx_bar("30m") == "30m"
    assert okx_bar("60m") == "1H"
    with pytest.raises(ValueError):
        okx_bar("1m")


def test_parse_keeps_closed_candles_and_quote_amount():
    payload = {
        "code": "0",
        "data": [
            _row(NOW, confirm="0"),
            _row(NOW - timedelta(minutes=30)),
            ["bad"],
        ],
    }
    bars = parse_okx_candle_payload(payload, symbol="BTC-USDT", freq="30m", received_at=NOW)
    assert len(bars) == 1
    bar = validate_bar(bars[0])
    assert bar.symbol == "BTC-USDT"
    assert bar.freq == "30m"
    assert bar.exchange_ts == NOW - timedelta(minutes=30)
    assert bar.local_recv_ts == NOW
    assert bar.open == 10
    assert bar.high == 11
    assert bar.low == 9
    assert bar.close == 10.5
    assert bar.volume == 1.5
    assert bar.amount == 16.2


def test_parse_rejects_okx_error():
    with pytest.raises(ValueError, match="OKX 错误"):
        parse_okx_candle_payload(
            {"code": "51001", "msg": "instId", "data": []},
            symbol="BTC-USDT",
            freq="60m",
            received_at=NOW,
        )


def test_fetch_pages_recent_then_history_and_drops_open_candle():
    recent_new = END - timedelta(minutes=30)
    recent_old = END - timedelta(minutes=60)
    older = END - timedelta(minutes=90)
    on_start = START
    too_old = START - timedelta(minutes=30)
    calls: list[str] = []

    def get_json(url: str) -> dict:
        calls.append(url)
        path = urlparse(url).path
        after = _after(url)
        if path.endswith("/market/candles"):
            if after == int(END.timestamp() * 1000):
                return {"code": "0", "data": [_row(recent_new, confirm="0"), _row(recent_old)]}
            return {"code": "0", "data": []}
        if after == int(recent_old.timestamp() * 1000):
            return {"code": "0", "data": [_row(older), _row(on_start), _row(too_old)]}
        return {"code": "0", "data": []}

    bars = fetch_okx_bars(
        "BTC-USDT",
        "30m",
        START,
        END,
        get_json=get_json,
        sleep_s=0,
        received_at=NOW,
    )
    assert [bar.exchange_ts for bar in bars] == [on_start, older, recent_old]
    paths = [urlparse(url).path for url in calls]
    assert paths[0].endswith("/market/candles")
    assert any(path.endswith("/market/history-candles") for path in paths)
    assert "bar=30m" in calls[0]


def test_fetch_60m_uses_okx_1h_bar():
    seen: list[str] = []

    def get_json(url: str) -> dict:
        seen.append(url)
        return {"code": "0", "data": []}

    fetch_okx_bars(
        "BTC-USDT",
        "60m",
        START,
        END,
        get_json=get_json,
        sleep_s=0,
        received_at=NOW,
    )
    assert any("bar=1H" in url for url in seen)


def test_backfill_inserts_without_opening_clickhouse():
    stamp = NOW - timedelta(hours=2)

    def get_json(url: str) -> dict:
        after = _after(url)
        if after == int(NOW.timestamp() * 1000):
            return {"code": "0", "data": [_row(stamp)]}
        return {"code": "0", "data": []}

    class Repo:
        def __init__(self) -> None:
            self.batches: list[list] = []

        def insert_bars(self, bars) -> int:
            self.batches.append(list(bars))
            return len(bars)

    repo = Repo()
    written = backfill(
        symbol="BTC-USDT",
        freqs=("60m",),
        months=24,
        now=NOW,
        repo=repo,
        get_json=get_json,
        sleep_s=0,
    )
    assert written == {"60m": 1}
    assert len(repo.batches) == 1
    assert repo.batches[0][0].freq == "60m"
    assert repo.batches[0][0].symbol == "BTC-USDT"


def test_insert_history_chunks():
    bars = parse_okx_candle_payload(
        {"code": "0", "data": [_row(NOW - timedelta(minutes=30 * i)) for i in range(5)]},
        symbol="BTC-USDT",
        freq="30m",
        received_at=NOW,
    )

    class Repo:
        def __init__(self) -> None:
            self.batches: list[int] = []

        def insert_bars(self, batch) -> int:
            self.batches.append(len(batch))
            return len(batch)

    repo = Repo()
    assert insert_history(repo, bars, chunk=2) == 5
    assert repo.batches == [2, 2, 1]


def _after(url: str) -> int | None:
    query = urlparse(url).query
    for part in query.split("&"):
        if part.startswith("after="):
            return int(part.split("=", 1)[1])
    return None
